"""P4/W1 T2.W1.3 — a test deletion that reaches disk IS a deletion that happened.

T3 adds the test-removal policy to the policy language. **I own the workspace
write path.** A policy rule the tool layer honours but the workspace backend
does not is a rule with a hole, and the hole is at the lowest layer — the one
that calls ``unlink``.

--------------------------------------------------------------------------
WHAT WAS ACTUALLY THERE, MEASURED
--------------------------------------------------------------------------

Before this phase, ``execution/workspace.py`` had **no test-file protection at
all**:

* ``_DEFAULT_PROTECTED_PATTERNS`` (``workspace.py:464``) is a list of SECRETS —
  ``.env``, ``*.pem``, ``id_rsa*``, ``credentials*`` — and not one test glob;
* ``execution.ecosystems.is_test_path`` exists and had **zero callers**
  anywhere in the tree, production or test;
* ``protected_paths`` defaults to ``()``, so a ``Workspace`` built without an
  explicit list protects nothing beyond VCS directories and secret filenames.

And the ways to remove a test that a ``Workspace`` does NOT block:

============================  =================================================
shape                         primitive
============================  =================================================
``delete_file``               ``Path.unlink()``
``write_file(content="")``    a zero-byte post-image through ``os.replace``
``apply_exact_edit(new="")``  a zero-byte post-image through ``os.replace``
``apply_unified_patch``       a file reduced to empty routes to ``delete_file``
``rename_file``               ``os.replace`` moves it out of collection
``undo`` of a create          ``_fenced_unlink`` on the agent-created file
``git checkout -- tests/``    a shell command, not a workspace call at all
``git clean``                 ditto
``@skip`` / ``xfail``         the test still exists and still does not run
``pytest.ini`` deselect       ditto, with the file left looking untouched
============================  =================================================

The last two rows are the ones a reviewer does not think of, and they are the
reason this module enumerates *shapes* rather than checking *paths*.

--------------------------------------------------------------------------
THE SKIP DECISION — made here, and T3 must make the same one
--------------------------------------------------------------------------

**A skip IS a deletion.** ``@pytest.mark.skip``, ``@pytest.mark.xfail``, a
``conftest.py`` ``collect_ignore``, an ``addopts = --deselect`` marker, or
removal from an explicit suite list all produce the identical observable
outcome as ``unlink``: the coverage stops running and the suite goes green
without it. The file's continued existence is a property of the filesystem, not
of what the suite checks.

So :data:`SKIP_DECISION` is ``"skip_is_deletion"``, the skip shapes are refused
on the same path as ``unlink``, and the ONE escape is a declared intent —
``declared=True`` — which is what ``harness/test_config.py``'s
``test_config_change`` already implements for test *configuration*. The
declaration produces a RECEIPT; it never produces silence.

--------------------------------------------------------------------------
THE USER/AGENT DISTINCTION HAS TO SURVIVE AT THIS LAYER
--------------------------------------------------------------------------

"An agent-initiated deletion is refused; a user-initiated one is not." If this
layer cannot tell them apart, the distinction is decorative. ``Workspace`` had
``PolicyContext.actor`` defaulting to ``"agent"`` and **no caller ever set
anything else**, so before this phase every deletion at the workspace layer was
indistinguishable.

:data:`ORIGINS` is the closed vocabulary. :func:`mutation_verdict` refuses an
``origin`` outside it — so a caller cannot pass an invented value and receive a
bypass. And ``Workspace`` gained a keyword-only ``mutation_origin`` that
travels onto the journal row, so an audit can tell afterwards which of the two
initiated a given write even though both were allowed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_ORIGIN",
    "DELETION_SHAPES",
    "DESELECT_CONFIG_TABLES",
    "ORIGINS",
    "ORIGIN_AGENT",
    "ORIGIN_USER",
    "SHAPES_THAT_ONLY_SKIP",
    "SHAPES_THAT_REACH_DISK",
    "SHAPE_CHMOD_DISABLED",
    "SHAPE_CONFTEST_IGNORE",
    "SHAPE_PATCH_TO_EMPTY",
    "SHAPE_RENAME_AWAY",
    "SHAPE_SCRIPT_TRUNCATION",
    "SHAPE_SKIP_DECORATOR",
    "SHAPE_SKIP_DESELECT",
    "SHAPE_SKIP_MARKER",
    "SHAPE_SUITE_LIST_REMOVAL",
    "SHAPE_TRUNCATE",
    "SHAPE_UNLINK",
    "SHAPE_VCS_CLEAN",
    "SHAPE_VCS_DELETE",
    "SHAPE_VCS_RESTORE",
    "SKIP_DECISION",
    "SKIP_DECISION_CONSEQUENCE",
    "SKIP_DECISION_REASON",
    "SKIP_MARKERS",
    "SUITE_LIST_FILES",
    "VCS_DELETION_COMMANDS",
    "DeletionVerdict",
    "MutationShape",
    "TestDeletionRefused",
    "assert_mutation_permitted",
    "classify_mutation",
    "command_deletion_verdict",
    "mutation_verdict",
    "render_verdict",
    "scan_source_for_skips",
    "skip_findings",
    "test_path_matcher",
]


# --------------------------------------------------------------------------
# Origin — the user/agent distinction, as a closed vocabulary
# --------------------------------------------------------------------------

ORIGIN_AGENT = "agent"
ORIGIN_USER = "user"

#: Closed. A caller that passes anything else gets a refusal, not a bypass: an
#: invented origin string must not be able to unlock the refusal.
ORIGINS: Tuple[str, str] = (ORIGIN_AGENT, ORIGIN_USER)

#: Fail toward the refusing side. An unspecified origin is an agent-initiated
#: mutation, because every mutating tool call in this product is one.
DEFAULT_ORIGIN = ORIGIN_AGENT


class TestDeletionRefused(PermissionError):
    """A test-removal mutation was refused before anything reached disk.

    A ``PermissionError`` deliberately: ``execution.workspace``'s own
    fail-closed paths raise it, so a caller already handling a refused mutation
    handles this one. It is deliberately NOT a ``ValueError`` — an unusable
    *argument* and a refused *effect* are different failures.
    """


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

SHAPE_UNLINK = "unlink"
SHAPE_TRUNCATE = "truncate"
SHAPE_PATCH_TO_EMPTY = "patch_to_empty"
SHAPE_RENAME_AWAY = "rename_away"
SHAPE_CHMOD_DISABLED = "chmod_disabled"
SHAPE_NO_TESTS_LEFT = "no_tests_left"
SHAPE_SKIP_DECORATOR = "skip_decorator"
SHAPE_SKIP_MARKER = "skip_marker"
SHAPE_SKIP_DESELECT = "skip_deselect"
SHAPE_SUITE_LIST_REMOVAL = "suite_list_removal"
SHAPE_CONFTEST_IGNORE = "conftest_ignore"
SHAPE_VCS_RESTORE = "vcs_restore"
SHAPE_VCS_CLEAN = "vcs_clean"
SHAPE_VCS_DELETE = "vcs_delete"
SHAPE_SCRIPT_TRUNCATION = "script_truncation"

#: Every shape this module recognises. A caller naming a shape outside it gets
#: a refusal, so a new shape cannot be introduced by typo and slip past.
DELETION_SHAPES: Tuple[str, ...] = (
    SHAPE_UNLINK,
    SHAPE_TRUNCATE,
    SHAPE_PATCH_TO_EMPTY,
    SHAPE_RENAME_AWAY,
    SHAPE_CHMOD_DISABLED,
    SHAPE_SKIP_DECORATOR,
    SHAPE_SKIP_MARKER,
    SHAPE_SKIP_DESELECT,
    SHAPE_SUITE_LIST_REMOVAL,
    SHAPE_CONFTEST_IGNORE,
    SHAPE_VCS_RESTORE,
    SHAPE_VCS_CLEAN,
    SHAPE_VCS_DELETE,
    SHAPE_SCRIPT_TRUNCATION,
    SHAPE_NO_TESTS_LEFT,
)

#: Shapes that remove a test's BYTES from disk.
SHAPES_THAT_REACH_DISK: Tuple[str, ...] = (
    SHAPE_UNLINK,
    SHAPE_TRUNCATE,
    SHAPE_PATCH_TO_EMPTY,
    SHAPE_RENAME_AWAY,
    SHAPE_VCS_RESTORE,
    SHAPE_VCS_CLEAN,
    SHAPE_VCS_DELETE,
    SHAPE_SCRIPT_TRUNCATION,
    SHAPE_NO_TESTS_LEFT,
)

#: Shapes that leave the bytes on disk and stop the suite running them. Under
#: :data:`SKIP_DECISION` these are still deletions.
SHAPES_THAT_ONLY_SKIP: Tuple[str, ...] = (
    SHAPE_SKIP_DECORATOR,
    SHAPE_SKIP_MARKER,
    SHAPE_SKIP_DESELECT,
    SHAPE_SUITE_LIST_REMOVAL,
    SHAPE_CONFTEST_IGNORE,
)


# --------------------------------------------------------------------------
# The skip decision, in one place, with its reasoning
# --------------------------------------------------------------------------

SKIP_DECISION = "skip_is_deletion"

SKIP_DECISION_REASON = (
    "a skip marker and a deletion produce the identical observable outcome: "
    "the coverage stops running and the suite goes green without it. The "
    "test file's continued existence on disk is a property of the filesystem, "
    "not of what the suite checks. So @skip, xfail, a pytest.ini deselect "
    "marker, a conftest collect_ignore, and removal from a suite list are "
    "refused on the same path as unlink."
)

SKIP_DECISION_CONSEQUENCE = (
    "this decision lives in execution/test_guard.py and is mirrored in the "
    "policy language T3 owns. THE TWO MUST AGREE. If T3 decides a skip is "
    "permitted with a receipt, this module must change in the same change; "
    "two answers to 'is a skip a deletion' is how a suite stops being evidence."
)


# --------------------------------------------------------------------------
# The skip vocabulary
# --------------------------------------------------------------------------

#: Regexes matched against source text. Deliberately conservative: a false
#: positive refuses an edit, and a refusal that fires on ordinary code is a
#: refusal that gets worked around.
SKIP_MARKERS: Tuple[Tuple[str, str], ...] = (
    (r"@pytest\.mark\.skip\b", "pytest skip marker"),
    (r"@pytest\.mark\.skipif\b", "pytest conditional skip marker"),
    (r"@pytest\.mark\.xfail\b", "pytest expected-failure marker"),
    (r"@unittest\.skip\b", "unittest skip decorator"),
    (r"@unittest\.expectedFailure\b", "unittest expected-failure decorator"),
    (r"^\s*(it|test|describe)\.(skip|todo)\b", "javascript test skip"),
    (r"@Ignore\b", "java junit @Ignore"),
    (r"@pytest\.mark\.usefixtures\b.*skip", "pytest skip via fixture"),
)

#: Files whose membership in a suite list is itself a declaration of what runs.
SUITE_LIST_FILES: Tuple[str, ...] = (
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
    "noxfile.py",
    "Makefile",
)

#: Configuration table names that carry test-selection options. A write to one
#: of these is examined for selection changes rather than blindly trusted.
DESELECT_CONFIG_TABLES: Tuple[str, ...] = (
    "tool.pytest.ini_options",
    "tool:pytest",
    "pytest",
    "test",
    "scripts.test",
    "jest",
    "karma",
)

#: Command prefixes that restore or remove tests WITHOUT going through the
#: workspace write path. Enumerated so the shell surface can be screened; a
#: `git checkout -- tests/` is a deletion that reaches disk through `git`.
VCS_DELETION_COMMANDS: Tuple[Tuple[str, str], ...] = (
    (r"^git\s+checkout\b.*(--\s*)?(?P<paths>.*)$", "git checkout restores bytes from HEAD"),
    (r"^git\s+restore\b.*$", "git restore restores bytes from the index"),
    (r"^git\s+clean\b.*$", "git clean removes untracked files"),
    (r"^git\s+rm\b.*$", "git rm removes tracked files"),
    (r"^git\s+stash\s+(drop|clear)\b.*$", "git stash drop discards a saved state"),
    (r"^git\s+reset\s+--hard\b.*$", "git reset --hard discards working-tree edits"),
    (r"^rm\b.*$", "rm removes files"),
    (r"^mv\b.*$", "mv can move a test out of collection"),
    (r"^truncate\b.*$", "truncate empties a file"),
    # P4/W2. `chmod_disabled` has been a published shape since this module
    # shipped, and the BYTES funnel catches it as a mode DELTA. The shell gate
    # had NO row for it, and the shell is the ONLY gate on `chmod`: a mode
    # change made by a command never reaches a workspace method, so an agent
    # that ran `chmod 000 tests/test_a.py` disabled a test while every funnel
    # stayed silent. Measured, in execution/test_removal_e2e.py: the command
    # `chmod 000 src/tests/test_checkout.py` classified as `ordinary_command`
    # and was allowed.
    #
    # Narrow on purpose: the row matches `chmod` with a MODE-CHANGING argument,
    # because `chmod +x scripts/run.sh` is a legitimate non-test edit and a
    # blanket `^chmod` row would refuse it. The test-path check that gates this
    # table still applies, so the refusal only fires on a test.
    #
    # The argument alternation covers an octal mode (`000`, `0644`), a symbolic
    # ADD (`+x`, `g+w`) and a symbolic REMOVE (`-x`, `go-w`).
    #
    # NO leading `\b` before the mode group, and that is load-bearing: a word
    # boundary requires a word character on one side, and the character before
    # `-x` is a SPACE, so `\b[+-]` never fires and `chmod -x` — the most common
    # way to make a file uncollectable — sailed through while `chmod 000` and
    # `chmod go-w` were refused. Two of three chmod shapes caught is a gate that
    # looks like it is working.
    (
        r"^chmod\b.*(?:0?[0-7]{3,4}|[ugoa]*[+-][rwxst]+).*$",
        "chmod changes a mode",
    ),
)

#: Command-line flags that remove cases from a runner's OWN denominator without
#: touching a single file. Each row is ``(pattern, why, needs_test_path)``.
#:
#: This table exists because the module's own docstring claims a deselect is
#: refused "on the same path as unlink" — and it was, for the CONFIG-FILE route
#: only (:data:`DESELECT_CONFIG_TABLES`). The command-line route had no row, so
#: `python -m pytest -q --deselect tests/test_a.py::test_a` was allowed, which is
#: the same coverage removal reached by a different door. Measured before the
#: fix: `refused=False, shape=ordinary_command`.
#:
#: Narrow on purpose, and the narrowness is the design: ``-k`` on its own is an
#: ordinary pytest expression filter and refusing every ``-k`` would make the
#: gate unusable. Only the forms that *name cases to exclude* are listed.
#:
#: ``needs_test_path`` is the third element and it is the subtle one. ``--ignore``
#: names a PATH, so it is only a coverage removal when that path is a test — and
#: ``--ignore htmlcov`` must stay allowed. The others name CASES, not paths:
#: ``-m 'not slow'`` excludes cases from whatever suite runs and mentions no path
#: at all, so the path gate cannot see it and applying the path gate anyway
#: leaves the flag allowed. Those rows are refused unconditionally, because a
#: marker filter that excludes cases IS a coverage removal by construction.
COVERAGE_REMOVING_FLAGS: Tuple[Tuple[str, str, bool], ...] = (
    (r"(?:^|\s)--deselect\b", "--deselect names a case to exclude", False),
    (r"(?:^|\s)--ignore\b", "--ignore removes a path from collection", True),
    (r"(?:^|\s)--collect-only\b", "--collect-only collects and never runs", False),
    (r"(?:^|\s)--co(?:\s|$)", "--co collects and never runs", False),
    (r"(?:^|\s)-m\s+[\"']?not\b", "-m 'not ...' excludes by marker", False),
    (r"(?:^|\s)-k\s+[\"']?not\b", "-k 'not ...' excludes by expression", False),
)

#: Command prefixes that MOVE a test file rather than restore or remove it.
#: Separate from :data:`VCS_DELETION_COMMANDS` because the shape it produces is
#: different: the bytes survive, the coverage does not.
_MOVE_COMMANDS: Tuple[Tuple[str, str], ...] = (
    (r"^mv\b.*$", "mv moves a file"),
    (r"^git\s+mv\b.*$", "git mv moves a tracked file"),
)


# --------------------------------------------------------------------------
# Test-path recognition
# --------------------------------------------------------------------------


def test_path_matcher(ecosystem: Optional[str] = None) -> Any:
    """Return the repository's test-path predicate, delegating when possible.

    ``execution.ecosystems.is_test_path`` is the authority and it is
    language-aware; it had ZERO callers before this phase. This function is how
    the workspace layer reaches it without forking it, because a second
    path-matcher in ``execution/`` is a second answer to "is this a test".

    A missing or broken registry degrades to the conservative built-in
    matcher below rather than to "nothing is protected".
    """
    try:
        from execution.ecosystems import detect_ecosystem, is_test_path
        from execution.ecosystems import ecosystem as _eco
    except Exception:  # pragma: no cover - defensive
        return _builtin_test_path
    found = None
    if ecosystem:
        found = _eco(ecosystem)
    else:
        found = detect_ecosystem(".")

    def matcher(relative: str) -> bool:
        target = found if found is not None else None
        if _matches_builtin(relative):
            return True
        if target is None:
            return False
        try:
            return bool(is_test_path(str(relative), target))
        except Exception:  # pragma: no cover - defensive
            return False

    matcher.source = "execution.ecosystems.is_test_path"  # type: ignore[attr-defined]
    matcher.builtin = _builtin_test_path  # type: ignore[attr-defined]
    matcher.eco = getattr(found, "name", "") if found is not None else ""  # type: ignore[attr-defined]
    return matcher


#: The shape :func:`classify_mutation` returns for a mutation that removes no
#: coverage. Named, not a literal, because it is a real answer and both
#: :func:`classify_mutation` and :func:`mutation_verdict` have to agree on it.
ORDINARY_MUTATION = "ordinary_mutation"

_TEST_DIR_SEGMENTS = frozenset(
    {"tests", "test", "spec", "specs", "__tests__", "testing", "e2e", "it"}
)
# Targets whose SCOPE is the entire tree. `.` names no test path, so a
# test-path check alone would clear them — and "every file" includes every test
# file, so clearing them on that basis is the hole.
#
# The trigger is the COMMAND plus a tree-wide flag, deliberately ignoring what
# token comes last. An earlier version anchored on `\s\.\s*$`, which missed
# `rm -rf ./` and every `git clean -fd` because the flag sits after the target.
# Pinning the anchor to the end of the command made the check depend on
# argument ORDER, which is not a property that should decide a refusal.
_WHOLE_TREE_TARGET = re.compile(
    r"\brm\b(?=[^|;&]*\s-{1,2}[a-z]*[rRfF][a-z]*\b)[^|;&]*(\s\.\s*\/?\s*$|\s\.\.\s*$|"
    r"\s/\s*$|\s\*\s*$|\s\.\s)|"
    r"\bgit\s+clean\b(?=[^|;&]*\s-[a-z]*[dfx][a-z]*\b)",
    re.IGNORECASE,
)
_TEST_FILE_RE = re.compile(
    r"(^test_.+|^.+_test$|^.+_tests$|^.+_spec$|^.+Test$|^.+Tests$|^.+Spec$|"
    r"^test_.+\.(py|js|ts|tsx|jsx|go|rb|java|kt|cs|php|swift)$|"
    r"^.+(_test|_spec)\.(py|js|ts|tsx|jsx|go|rb|java|kt|cs|php|swift)$)",
    re.IGNORECASE,
)
_TEST_FUNCTION_RE = re.compile(r"^\s*(?:async\s+)?(?:def\s+test_|it\(|test\(|describe\()")

#: Runner BINARY names. A file called ``pytest`` is not a test file; the word is
#: a command, and a command is not a path.
#:
#: P4/W2 found this. ``_matches_builtin("pytest")`` returned **True**, so the
#: command-gate's own path-token reader classified ``python -m pytest -q
#: --ignore htmlcov`` as naming a test path (the token it picked up was the
#: runner, not the argument) and refused it. The false positive predates this
#: round — but it was INVISIBLE, because nothing matched a coverage-removing
#: flag, so the buggy path token was never consulted for a command that had no
#: deletion shape. Adding the deselect row made a three-year-old reader bug
#: visible in one line, which is the argument for driving the real classifier
#: rather than the real workspace.
#:
#: A file named ``test_pytest.py`` still matches, via :data:`_TEST_FILE_RE`.
_RUNNER_BINARY_NAMES = frozenset(
    {
        "pytest",
        "py.test",
        "jest",
        "vitest",
        "mocha",
        "tox",
        "nox",
        "nose",
        "rspec",
        "unittest",
        "karma",
        "phpunit",
    }
)


def _matches_builtin(relative: str) -> bool:
    text = str(relative or "").replace("\\", "/").strip("/")
    if not text:
        return False
    segments = text.split("/")
    lowered = [segment.lower() for segment in segments]
    # A single bare segment naming a runner is a COMMAND, not a path. Checked
    # before the directory-segment test because `pytest` appears in a command as
    # a bare word and never as a path component.
    if len(segments) == 1 and lowered[0] in _RUNNER_BINARY_NAMES:
        return False
    if any(segment in _TEST_DIR_SEGMENTS for segment in lowered[:-1]):
        return True
    basename = segments[-1]
    if basename.lower() in {"conftest.py", "conftest.js", "conftest.ts"}:
        return True
    return bool(_TEST_FILE_RE.match(basename))


def _builtin_test_path(relative: str) -> bool:
    """The conservative fallback matcher, used when the registry is unreachable."""
    return _matches_builtin(relative)


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MutationShape:
    """What kind of test-removal this is, derived rather than declared.

    ``classify_mutation`` derives the shape from the workspace mutation's own
    facts (kind, pre-image, post-image, destination). A caller cannot hand it
    ``shape="unlink"`` for a write, because the shape is computed from the
    bytes — the same discipline ``execution.edit_landing.landing_receipt``
    uses, where the receipt computes its verdict instead of accepting one.
    """

    shape: str
    reaches_disk: bool
    detail: str = ""
    secondary: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shape": self.shape,
            "reaches_disk": bool(self.reaches_disk),
            "detail": self.detail,
            "secondary": list(self.secondary),
        }


@dataclass(frozen=True)
class DeletionVerdict:
    """Whether a mutation is permitted, and everything a reader needs to know.

    ``allowed`` is the answer. ``shape``, ``origin``, ``reason`` and
    ``declared`` are the receipt — because a refusal a caller cannot read is a
    refusal a caller works around.
    """

    allowed: bool
    shape: str
    origin: str
    reason: str
    path: str = ""
    detail: str = ""
    reaches_disk: bool = False
    is_test_path: bool = False
    declared: bool = False
    skip_decision: str = SKIP_DECISION
    notes: Tuple[str, ...] = ()
    receipt: Dict[str, Any] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return not self.allowed

    @property
    def user_initiated(self) -> bool:
        return self.origin == ORIGIN_USER

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "refused": bool(self.refused),
            "shape": self.shape,
            "origin": self.origin,
            "user_initiated": self.user_initiated,
            "reason": self.reason,
            "path": self.path,
            "detail": self.detail,
            "reaches_disk": bool(self.reaches_disk),
            "is_test_path": bool(self.is_test_path),
            "declared": bool(self.declared),
            "skip_decision": self.skip_decision,
            "notes": list(self.notes),
            "receipt": dict(self.receipt),
        }

    def render(self) -> List[str]:
        head = "PERMITTED" if self.allowed else "REFUSED"
        lines = [
            f"{head}: {self.shape} on {self.path or '(no path)'} "
            f"[origin={self.origin}]"
        ]
        lines.append(f"  reason       : {self.reason}")
        if self.detail:
            lines.append(f"  detail       : {self.detail}")
        lines.append(f"  reaches disk : {self.reaches_disk}")
        lines.append(f"  is a test    : {self.is_test_path}")
        lines.append(f"  declared     : {self.declared}")
        lines.append(f"  skip policy  : {self.skip_decision}")
        for note in self.notes:
            lines.append(f"  note         : {note}")
        return lines


# --------------------------------------------------------------------------
# Shape classification
# --------------------------------------------------------------------------

#: ``Workspace`` mutation kinds. Four literals, from ``_mutation.kind``.
_KIND_WRITE = "write"
_KIND_EDIT = "edit"
_KIND_DELETE = "delete"
_KIND_RENAME = "rename"


def classify_mutation(
    *,
    kind: str,
    relative_path: str,
    before_bytes: Optional[bytes],
    after_bytes: Optional[bytes],
    destination: Optional[str] = None,
    mode_after: Optional[int] = None,
    mode_before: Optional[int] = None,
) -> MutationShape:
    """Derive the deletion shape of one workspace mutation.

    The shape comes from the bytes and the kind, never from the caller's word
    for it:

    * ``delete`` → ``unlink``;
    * ``rename`` whose DESTINATION is no longer a test path → ``rename_away``;
    * a write/edit whose post-image is empty while the pre-image was not →
      ``truncate`` (and ``patch_to_empty`` when the caller says it arrived by
      patch, which is the same shape named from its route);
    * a mode that LOST a bit the previous mode had → ``chmod_disabled``.

    **The chmod check is a DELTA, and it has to be.** An absolute test — "does
    the new mode have the execute bit?" — fires on Windows, where no file ever
    carries one, so it classified every ordinary edit to a test as
    ``chmod_disabled``. That is the failure mode this repository names
    repeatedly: a check that fires on working code is a check that gets deleted
    rather than satisfied. Only a bit the PREVIOUS mode had and the new mode
    lacks is a loss, and a mutation whose mode did not change at all is not a
    chmod.

    A non-test path is still classified — the shape is a fact about the
    mutation — and the CALLER decides whether it matters.
    """
    text = str(kind or "").strip().lower()
    before = bytes(before_bytes) if before_bytes is not None else None
    after = bytes(after_bytes) if after_bytes is not None else None

    if text == _KIND_DELETE:
        return MutationShape(
            shape=SHAPE_UNLINK,
            reaches_disk=True,
            detail=f"{relative_path} was deleted outright",
        )
    if text == _KIND_RENAME:
        source_is_test = _matches_builtin(relative_path)
        dest = str(destination or "")
        dest_is_test = _matches_builtin(dest) if dest else False
        if source_is_test and not dest_is_test:
            return MutationShape(
                shape=SHAPE_RENAME_AWAY,
                reaches_disk=True,
                detail=(
                    f"{relative_path} would move to {dest or '(no destination)'}, "
                    f"which is not a test path, so it leaves test collection "
                    f"while its bytes stay on disk"
                ),
            )
        return MutationShape(
            shape=SHAPE_RENAME_AWAY,
            reaches_disk=False,
            detail=f"{relative_path} -> {dest}: a rename between two test paths",
        )
    if text in (_KIND_WRITE, _KIND_EDIT):
        if after is not None and len(after) == 0 and before:
            return MutationShape(
                shape=SHAPE_TRUNCATE,
                reaches_disk=True,
                detail=(
                    f"{relative_path} would go from {len(before)} byte(s) to an "
                    f"empty file. A test file with no bytes collects nothing, so "
                    f"the suite goes green without running it"
                ),
            )
        # The vacuous-green shape: the file is NOT emptied, but every test it
        # DECLARED is gone, so it still collects, still passes, and now checks
        # nothing. Detecting it is the difference between refusing a deletion
        # and refusing a deletion that disguised itself.
        #
        # THE BOUNDARY, STATED: this counts DECLARATIONS, not bodies. An edit
        # that empties a test's body while leaving `def test_a():` in place
        # leaves a test that still runs and still passes — a *trivial* test, not
        # a deleted one, and refusing it would be a guard that fires on
        # ordinary work. The repository's own `harness/test_config.py` guard
        # and a code review own that judgement; this module owns "the test is
        # gone".
        if after is not None and before:
            had = _count_test_declarations(before)
            now = _count_test_declarations(after)
            if had > 0 and now == 0:
                return MutationShape(
                    shape=SHAPE_NO_TESTS_LEFT,
                    reaches_disk=True,
                    detail=(
                        f"{relative_path} declared {had} test(s) and would "
                        f"declare none. The file still collects and still passes, "
                        f"so the suite goes green while checking nothing — the "
                        f"vacuous-green shape, which is a deletion wearing a "
                        f"signature"
                    ),
                )
    lost_bits, lost_detail = _lost_mode_bits(mode_before, mode_after)
    if lost_bits:
        return MutationShape(
            shape=SHAPE_CHMOD_DISABLED,
            reaches_disk=True,
            detail=f"{relative_path} {lost_detail}, which makes a test unrunnable",
        )
    return MutationShape(
        shape="ordinary_mutation",
        reaches_disk=False,
        detail=f"{relative_path}: a {text} that removes no coverage",
    )


def _count_test_declarations(source: bytes) -> int:
    """How many test declarations a file's bytes carry.

    Counts by text, deliberately, and over several languages' shapes rather
    than one grammar: the same property has to hold for a Python ``def
    test_``/``class Test``, a JavaScript ``it(``/``test(``, a Go ``func
    Test``, a Ruby ``it '`` and a Java ``@Test``. An AST pass would answer it
    for exactly one of them and be wrong about the rest.

    A source that does not decode is counted as ``-1`` — "could not tell" — so
    a binary or latin-1 file is never reported as having zero tests, which is
    the direction that would refuse an edit for no reason.
    """
    try:
        text = bytes(source).decode("utf-8")
    except (UnicodeDecodeError, AttributeError, ValueError):
        return -1
    patterns = (
        r"^\s*(?:async\s+)?def\s+test_\w+",
        r"^\s*class\s+Test\w*\s*[\(:]",
        r"^\s*(?:async\s+)?def\s+it\s*\(",
        r"^\s*func\s+Test\w+\s*\(",
        r"^\s*it\s*[\(\s]",
        r"^\s*test\s*\(",
        r"^\s*describe\s*\(",
        r"@Test\b",
        r"^\s*it\s+'",
    )
    found = 0
    for line in text.splitlines():
        if any(re.search(pattern, line) for pattern in patterns):
            found += 1
    return found


def _lost_mode_bits(
    mode_before: Optional[int], mode_after: Optional[int]
) -> Tuple[int, str]:
    """Which bits the new mode dropped relative to the old one, and a phrase.

    Returns ``(0, "")`` when nothing was lost, including the case where the
    mode did not change at all — which is the common case and the one an
    absolute check got wrong.
    """
    import stat as _stat

    if mode_after is None or mode_before is None:
        return 0, ""
    try:
        old = int(mode_before)
        new = int(mode_after)
    except (TypeError, ValueError):
        return 0, ""
    if old == new:
        return 0, ""
    lost = 0
    words: List[str] = []
    for bit, word in ((_stat.S_IREAD, "read"), (_stat.S_IEXEC, "execute")):
        if (old & bit) and not (new & bit):
            lost |= bit
            words.append(word)
    if not lost:
        return 0, ""
    phrase = " and ".join(words)
    return lost, f"would lose its {phrase} bit(s)"


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------


def mutation_verdict(
    *,
    shape: str,
    relative_path: str,
    origin: str = DEFAULT_ORIGIN,
    declared: bool = False,
    is_test: Optional[bool] = None,
    detail: str = "",
    notes: Sequence[str] = (),
) -> DeletionVerdict:
    """Decide whether one test-removal mutation is permitted.

    The decision order, and each step is load-bearing:

    1. **an unknown shape is a REFUSAL.** A caller that invents a shape, or a
       future edit that adds one without adding it to :data:`DELETION_SHAPES`,
       must not be able to slip past a guard by spelling it differently;
    2. **an unknown origin is a REFUSAL.** This is what makes the user/agent
       distinction survive at this layer: ``origin="operator"`` is not a
       synonym for ``origin="user"``, and a typo cannot become a bypass;
    3. **a shape that is not a deletion shape is ALLOWED** — an ordinary edit to
       a test file (adding a case, fixing a fixture) must not be refused, or
       the guard is a wall rather than a rule and gets removed wholesale;
    4. **the path is the next question.** A deletion of ``src/app.py`` is not a
       test deletion;
    5. **origin decides.** An agent-initiated deletion of a test is REFUSED. A
       user-initiated one is ALLOWED, because "the user can still do it" is a
       requirement, not an oversight;
    6. **a declared intent is the ONE escape**, and it produces a receipt. A
       declared deletion is still reported; it is simply not refused.

    ``is_test`` exists so a caller that has ALREADY classified the path does
    not pay for a second classification, and so the verdict can be pinned with
    a known answer.
    """
    named = str(shape or "")
    where = str(relative_path or "")
    who = str(origin or "")
    extra = [str(item) for item in (notes or ())]

    # `ordinary_mutation` is a shape this module PRODUCES (see `classify_mutation`)
    # and it means "a mutation that removes no coverage". Treating it as an
    # unrecognised shape and refusing it is a real defect the pin suite caught:
    # `mutation_verdict` is public, so any caller passing a legitimately
    # ordinary write — including one that classified it itself — got refused.
    #
    # The workspace path never hit it because `assert_mutation_permitted`
    # short-circuits non-deletion shapes before calling here. That is exactly
    # the "the production call site does not exercise the public function, so
    # the bug is invisible until someone else calls it" shape this project has
    # paid for repeatedly.
    if named == ORDINARY_MUTATION:
        return DeletionVerdict(
            allowed=True,
            shape=named,
            origin=who or DEFAULT_ORIGIN,
            reason=(
                f"`{where or 'the target'}` is not a test-removal shape, so this "
                f"mutation is not this guard's business"
                + (f". {detail}" if detail else "")
            ),
            path=where,
            detail=detail,
            is_test_path=False,
            declared=False,
            reaches_disk=False,
            notes=tuple(extra),
            receipt={"shape": named, "not_a_test_removal": True},
        )

    if named not in DELETION_SHAPES:
        return DeletionVerdict(
            allowed=False,
            shape=named or "<empty>",
            origin=who or "<empty>",
            reason=(
                f"unknown test-deletion shape {named!r}. The vocabulary is "
                f"closed ({len(DELETION_SHAPES)} shapes), and an unrecognised "
                f"shape is REFUSED rather than assumed harmless: a guard whose "
                f"vocabulary can be extended by a typo is not a guard."
            ),
            path=where,
            notes=extra,
            receipt={"known_shapes": list(DELETION_SHAPES)},
        )
    if who not in ORIGINS:
        return DeletionVerdict(
            allowed=False,
            shape=named,
            origin=who or "<empty>",
            reason=(
                f"unknown mutation origin {who!r}. The vocabulary is closed "
                f"({ORIGINS}). An invented origin must not be able to unlock a "
                f"refusal, so this is refused before the shape is even examined."
            ),
            path=where,
            notes=extra,
            receipt={"known_origins": list(ORIGINS)},
        )

    if named == "ordinary_mutation":
        return DeletionVerdict(
            allowed=True,
            shape=named,
            origin=who,
            reason="this mutation removes no test coverage, so it is not guarded",
            path=where,
            detail=detail,
            notes=extra,
        )

    test_like = _matches_builtin(where) if is_test is None else bool(is_test)
    if not test_like:
        return DeletionVerdict(
            allowed=True,
            shape=named,
            origin=who,
            reason=(
                f"{where or 'this path'} is not a test path, so this mutation is "
                f"not a test deletion. The shape is still reported: "
                f"{named}"
            ),
            path=where,
            reaches_disk=named in SHAPES_THAT_REACH_DISK,
            is_test_path=False,
            detail=detail,
            notes=extra,
        )

    reaches_disk = named in SHAPES_THAT_REACH_DISK
    only_skip = named in SHAPES_THAT_ONLY_SKIP
    shape_line = detail or (
        f"{where} would be removed via {named}"
        + ("" if reaches_disk else " (the bytes stay; the coverage stops running)")
    )

    if who == ORIGIN_USER:
        return DeletionVerdict(
            allowed=True,
            shape=named,
            origin=who,
            reason=(
                f"a USER-initiated test removal is not refused. The "
                f"user/agent distinction is a requirement, not an oversight: "
                f"{shape_line}. This write is journalled with origin={who!r} so "
                f"an audit can tell it apart from an agent-initiated one."
            ),
            path=where,
            reaches_disk=reaches_disk,
            is_test_path=True,
            declared=bool(declared),
            notes=[*extra, f"user-initiated {named} on a test path: allowed and recorded"],
            receipt={"journalled_with_origin": who},
        )

    # -- agent-initiated, on a test path ---------------------------------
    if declared:
        return DeletionVerdict(
            allowed=True,
            shape=named,
            origin=who,
            reason=(
                f"DECLARED: the caller stated an intent to change test "
                f"coverage, so the removal proceeds. A declaration is a "
                f"RECEIPT, not a silent bypass: {shape_line}. The declaration "
                f"does not make the suite cover anything again."
            ),
            path=where,
            reaches_disk=reaches_disk,
            is_test_path=True,
            declared=True,
            notes=[*extra, "a declared change is recorded, never silent"],
            receipt={"declared": True, "shape": named},
        )

    why = (
        "removing or disabling a test removes the coverage the suite is "
        "supposed to provide, and the suite going green without it is "
        "indistinguishable from a feature that works"
    )
    if only_skip:
        why = SKIP_DECISION_REASON

    return DeletionVerdict(
        allowed=False,
        shape=named,
        origin=who,
        reason=(
            f"refused an AGENT-initiated test removal on {where}: {why}. "
            f"Shape: {named} — {shape_line}. A declared intent (declared=True) "
            f"is the one way through, and a USER-initiated removal is not "
            f"refused at all."
        ),
        path=where,
        reaches_disk=reaches_disk,
        is_test_path=True,
        declared=False,
        notes=tuple(extra)
        + (
            (SKIP_DECISION_CONSEQUENCE,) if only_skip else ()
        ),
        receipt={
            "shapes_that_reach_disk": list(SHAPES_THAT_REACH_DISK),
            "shapes_that_only_skip": list(SHAPES_THAT_ONLY_SKIP),
            "skip_decision": SKIP_DECISION,
        },
    )


def assert_mutation_permitted(**kwargs: Any) -> MutationShape:
    """Raise :class:`TestDeletionRefused` when the mutation is refused.

    The one call a workspace write path makes. Returns the classified
    :class:`MutationShape` on success so a caller can journal it.

    The refusal is raised BEFORE any byte is written and carries the full
    reason, so the operator reads *what kind of removal was attempted* and not
    "protected path refused".
    """
    shape = classify_mutation(
        kind=kwargs.get("kind", ""),
        relative_path=kwargs.get("relative_path", ""),
        before_bytes=kwargs.get("before_bytes"),
        after_bytes=kwargs.get("after_bytes"),
        destination=kwargs.get("destination"),
        mode_after=kwargs.get("mode_after"),
        mode_before=kwargs.get("mode_before"),
    )
    if not _is_deletion_shape(shape.shape):
        return shape
    verdict = mutation_verdict(
        shape=shape.shape,
        relative_path=kwargs.get("relative_path", ""),
        origin=kwargs.get("origin", DEFAULT_ORIGIN),
        declared=bool(kwargs.get("declared", False)),
        detail=shape.detail,
        notes=kwargs.get("notes", ()),
    )
    if verdict.refused:
        raise TestDeletionRefused(verdict.reason)
    return shape


def _is_deletion_shape(shape: str) -> bool:
    return shape in DELETION_SHAPES


# --------------------------------------------------------------------------
# Source scanning — the skip shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SkipFinding:
    """One skip marker found in a source file."""

    path: str
    line: int
    marker: str
    text: str
    shape: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "marker": self.marker,
            "text": self.text[:200],
            "shape": self.shape,
        }


def scan_source_for_skips(text: str, path: str = "") -> Tuple[SkipFinding, ...]:
    """Find skip markers in one file's source.

    Scans the source TEXT rather than the AST on purpose: ``@skip`` reaches a
    file through a decorator, a comment (``# @pytest.mark.skip`` left behind by
    a hurried edit), a string in a generated file, and a marker in a non-Python
    file the repo may also collect. An AST pass would see only the first and
    would need a second grammar for the fourth.

    Docstrings are NOT excluded, deliberately: a docstring that *documents* a
    skip is usually a fixture comment, and over-refusing an edit is the
    failure mode that gets a guard removed.
    """
    body = str(text or "")
    if not body:
        return ()
    found: List[SkipFinding] = []
    for number, line in enumerate(body.splitlines(), start=1):
        for pattern, label in SKIP_MARKERS:
            if re.search(pattern, line, re.MULTILINE):
                found.append(
                    SkipFinding(
                        path=str(path),
                        line=number,
                        marker=label,
                        text=line.strip(),
                        shape=SHAPE_SKIP_DECORATOR,
                    )
                )
                break
    return tuple(found)


def skip_findings(before_text: str, after_text: str, path: str = "") -> Dict[str, Any]:
    """The skip markers a write ADDS, and the ones it removes.

    A write that *removes* a skip is a coverage RESTORATION and must not be
    refused — refusing it would make the guard actively harmful. So this
    reports the delta, and the refusal keys on additions only.
    """
    before = {
        (row.line, row.marker): row for row in scan_source_for_skips(before_text, path)
    }
    after = {
        (row.line, row.marker): row for row in scan_source_for_skips(after_text, path)
    }
    added = tuple(after[key] for key in sorted(after) if key not in before)
    removed = tuple(before[key] for key in sorted(before) if key not in after)
    return {
        "path": str(path),
        "added": [row.to_dict() for row in added],
        "removed": [row.to_dict() for row in removed],
        "added_count": len(added),
        "removed_count": len(removed),
        "shape": SHAPE_SKIP_DECORATOR if added else "ordinary_mutation",
        "is_removal_of_coverage": bool(added),
        "is_restoration_of_coverage": bool(removed) and not added,
        "note": (
            "a write that removes a skip marker restores coverage and is NOT "
            "refused; refusing it would make this guard harmful, because the "
            "obvious fix for a refused skip is to remove the guard"
        ),
    }


# --------------------------------------------------------------------------
# The shell surface — git and rm never touch the workspace API
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandDeletionVerdict:
    """Whether a shell command removes tests without going through a workspace call."""

    refused: bool
    shape: str
    reason: str
    command: str = ""
    origin: str = DEFAULT_ORIGIN
    paths: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "refused": bool(self.refused),
            "shape": self.shape,
            "reason": self.reason,
            "command": self.command,
            "origin": self.origin,
            "paths": list(self.paths),
        }

    def render(self) -> List[str]:
        return [
            f"{'REFUSED' if self.refused else 'PERMITTED'}: {self.shape} via shell",
            f"  command : {self.command}",
            f"  origin  : {self.origin}",
            f"  reason  : {self.reason}",
        ]


def command_deletion_verdict(
    command: str,
    *,
    origin: str = DEFAULT_ORIGIN,
    declared: bool = False,
    paths: Optional[Sequence[str]] = None,
) -> CommandDeletionVerdict:
    """Screen a shell command for test removals that bypass the workspace.

    ``git checkout -- tests/``, ``git clean -fd``, ``git restore tests/`` and
    ``git reset --hard`` all restore or delete test bytes **without ever calling
    a workspace method**, so a workspace-level guard cannot see them. This is
    the other half of T2.W1.3's requirement and the reason the shapes
    ``vcs_restore`` / ``vcs_clean`` / ``vcs_delete`` / ``script_truncation``
    exist at all.

    Conservative in the RIGHT direction: a command whose *effect* on tests is
    unknown is treated as a possible removal, because the alternative — a
    shell-side bypass nobody screens — is the hole this closes.
    """
    text = str(command or "").strip()
    who = str(origin or DEFAULT_ORIGIN)
    if who not in ORIGINS:
        return CommandDeletionVerdict(
            refused=True,
            shape="unknown_origin",
            reason=(
                f"unknown mutation origin {who!r}; the vocabulary is closed "
                f"({ORIGINS}). An invented origin must not unlock a bypass."
            ),
            command=text,
            origin=who,
        )

    lowered = text.lower()

    # None means no coverage-removing flag matched. A flag that names CASES
    # rather than paths sets it to False, and that suppresses the test-path
    # gate further down -- see the note at that branch.
    coverage_flag_needs_path = None
    shape = ""
    reason = ""
    named_paths: Tuple[str, ...] = tuple(str(item) for item in (paths or ()))
    if not named_paths:
        # Read the command's own path-like tokens.
        #
        # The FIRST draft accepted a token only when it carried a separator or
        # its first segment was a known test directory. That made `rm app.py`
        # name NO path at all — "app.py" has neither a slash nor a test
        # directory as its head — and the "named paths, none of them a test"
        # branch below therefore never fired, so an ordinary non-test deletion
        # was refused as a test removal. A guard that refuses `rm app.py` is a
        # guard that gets disabled, and the requirement is specifically about
        # TEST removal.
        #
        # So the rule is: every non-flag, non-verb token is a candidate path.
        # The command word and its subcommand-ish leading flags are excluded,
        # and the refusal for a genuinely UNIDENTIFIABLE target is preserved as
        # its own branch below.
        head = lowered.split()[0] if lowered.split() else ""
        for token in text.split():
            cleaned = token.strip("\"',;")
            if not cleaned or cleaned.startswith("-"):
                continue
            if cleaned.lower() == head:
                continue
            named_paths = (cleaned,)
            break

    for pattern, why in _MOVE_COMMANDS:
        if re.search(pattern, text, re.IGNORECASE):
            shape = SHAPE_RENAME_AWAY
            reason = (
                f"{why} out of test collection: `{text}` can leave the bytes on "
                f"disk and remove the coverage at the same time, which is the "
                f"same observable outcome as a deletion"
            )
            break
    else:
        for pattern, why in VCS_DELETION_COMMANDS:
            if re.search(pattern, text, re.IGNORECASE):
                shape = (
                    SHAPE_CHMOD_DISABLED
                    if lowered.split()[0] == "chmod"
                    else SHAPE_VCS_CLEAN
                    if "clean" in lowered
                    else SHAPE_VCS_DELETE
                    if "rm" in lowered or lowered.startswith("rm")
                    else SHAPE_VCS_RESTORE
                )
                reason = (
                    f"{why}: `{text}` removes or restores repository bytes "
                    f"through {lowered.split()[0]!r}"
                )
                break
        else:
            if re.search(r">\s*\S*test\S*\.(py|js|ts|go|rb|java)", text, re.IGNORECASE) or (
                re.search(r":\s*>\s*\S*test", text, re.IGNORECASE)
            ):
                shape = SHAPE_SCRIPT_TRUNCATION
                reason = (
                    "a shell redirection writes over a test file, which empties or "
                    "replaces it without any workspace call: a deletion through the "
                    "shell"
                )
            elif re.search(r"\b(sed|tee|dd)\b.*test", lowered):
                shape = SHAPE_SCRIPT_TRUNCATION
                reason = (
                    "an in-place edit tool targets a test file; whether it removes "
                    "coverage depends on the edit, so it is treated as a possible "
                    "removal rather than assumed harmless"
                )
            else:
                # P4/W2. The deselect route. `--deselect tests/test_a.py::test_a`
                # removes a case from the denominator without touching a single
                # file, so every funnel stays silent and the suite goes green.
                # Measured before this branch existed: `refused=False,
                # shape=ordinary_command`.
                #
                # The flag is only half the evidence — it is matched HERE, inside
                # the branch that is already gated on `touches_tests` below, so
                # `pytest -q --ignore htmlcov` stays allowed while
                # `pytest -q --ignore tests/` does not.
                for pattern, why, needs_path in COVERAGE_REMOVING_FLAGS:
                    if re.search(pattern, text, re.IGNORECASE):
                        shape = (
                            SHAPE_SKIP_DECORATOR
                            if "skipif" in lowered or "xfail" in lowered
                            else SHAPE_SKIP_DESELECT
                        )
                        reason = (
                            f"{why}: `{text}` removes coverage from the runner's own "
                            f"denominator without changing a file. A skipped test is a "
                            f"deleted test's observable behaviour — {SKIP_DECISION_REASON}"
                        )
                        coverage_flag_needs_path = bool(needs_path)
                        break

    if not shape:
        return CommandDeletionVerdict(
            refused=False,
            shape="ordinary_command",
            reason="this command matches no known test-removal shape",
            command=text,
            origin=who,
            paths=named_paths,
        )

    touches_tests = any(_matches_builtin(item) for item in named_paths) or bool(
        re.search(r"\b(tests?|spec|__tests__)\b", lowered)
    )
    # A target that names NO test path but whose SCOPE is the whole tree cannot
    # be cleared by the test-path test, because "every file" includes every
    # test. `rm -rf .` and `git clean -fd` were both slipping through this
    # branch on the strength of naming `.`, which is not a test path — a hole
    # in exactly the direction this guard exists for.
    unbounded_target = (
        bool(_WHOLE_TREE_TARGET.search(text))
        and not re.search(r"\bgit\s+clean\b[^|;&]*\s-n\b", text, re.IGNORECASE)
    )
    if named_paths and not touches_tests and not unbounded_target and not (
        coverage_flag_needs_path is False and shape in SHAPES_THAT_ONLY_SKIP
    ):
        # Named paths and NONE of them is a test path: the command is a real
        # deletion but not a TEST deletion, so it is not this guard's business.
        #
        # The one exception is a coverage-removing flag that names CASES rather
        # than paths. `-m 'not slow'` names no path at all, so `touches_tests`
        # is False and clearing on that basis would ALLOW it — which is how it
        # stayed allowed through two rounds. A marker filter that excludes cases
        # is a coverage removal by construction, so the path gate does not apply
        # to it. `--ignore` keeps the gate, because it names a path and
        # `--ignore htmlcov` must stay allowed.
        return CommandDeletionVerdict(
            refused=False,
            shape=shape,
            reason=(
                f"the command is a {shape} but none of its named paths "
                f"({', '.join(named_paths)}) is a test path"
            ),
            command=text,
            origin=who,
            paths=named_paths,
        )
    if not named_paths or unbounded_target:
        extra_note = (
            " The command's target is not bounded to a named file — it removes "
            "every file the rule can reach, which includes every test file and "
            "any test this run has just written — so 'none of the paths is a "
            "test path' cannot be established."
        )
    else:
        extra_note = ""

    if who == ORIGIN_USER:
        return CommandDeletionVerdict(
            refused=False,
            shape=shape,
            reason=(
                f"a USER-initiated shell removal is not refused; it is "
                f"recorded with origin={who!r}. {reason}.{extra_note}"
            ),
            command=text,
            origin=who,
            paths=named_paths,
        )
    if declared:
        return CommandDeletionVerdict(
            refused=False,
            shape=shape,
            reason=(
                f"DECLARED: the caller stated an intent to change test "
                f"coverage, so this proceeds as a receipt. {reason}.{extra_note}"
            ),
            command=text,
            origin=who,
            paths=named_paths,
        )
    return CommandDeletionVerdict(
        refused=True,
        shape=shape,
        reason=(
            f"refused an AGENT-initiated test removal through the shell: "
            f"{reason}.{extra_note} A workspace-level guard cannot see this "
            f"path, which is exactly why it is screened here. A declared "
            f"intent is the one way through; a USER-initiated removal is not "
            f"refused."
        ),
        command=text,
        origin=who,
        paths=named_paths,
    )


def render_verdict(verdict: Any) -> List[str]:
    """Render either verdict type as plain lines."""
    render = getattr(verdict, "render", None)
    if callable(render):
        return list(render())
    return [str(verdict)]  # pragma: no cover - defensive


def _ast_probe(source: str) -> bool:  # pragma: no cover - documentation helper
    """Whether ``ast`` can parse ``source``.

    Exists so a future edit that wants an AST-based skip detector has the
    capability check in one place. This module scans text on purpose (see
    :func:`scan_source_for_skips`), so the probe is documentation, not a gate.
    """
    try:
        ast.parse(source)
        return True
    except (SyntaxError, ValueError):
        return False
