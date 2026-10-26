"""The agent may not remove or disable a test. Absolute, not approval-gated.

Anthropic's rule, quoted: *"It is unacceptable to remove or edit tests because
this could lead to missing or buggy functionality."* This module turns it from a
prompt line into a **policy**, for two reasons a prompt cannot cover:

* A prompt is a suggestion to a model that is also being fed untrusted text
  (issue bodies, web results, tool output). ``phases/DOCTRINE.md`` §5 lists both
  as untrusted sources, and neither is scanned for the instruction that tells the
  model to edit tests.
* An approval dialog is a human being asked to approve a test deletion. **An
  approval-gated test deletion is a test deletion.** There is deliberately no
  approval branch here, so there is nothing to configure and nothing to
  "temporarily" enable.

**The user can, explicitly.** That distinction is structural rather than a flag,
because a flag an agent could set would not be a distinction. :class:`Initiator`
is frozen, its ``user_initiated`` constructor is only reachable from
:func:`user_initiator`, and :func:`agent_initiator` returns an instance whose
``user_initiated`` is ``False`` with no parameter to change it. A tool call
crossing the agent boundary carries a :class:`ToolCall`, which has no field that
could hold an initiator at all — so there is nothing for the agent to write into,
because the type it is given has nowhere to put it.

**Skipping is deletion.** ``@pytest.mark.skip``, ``@pytest.mark.xfail``, a
``pytest.ini`` ``addopts`` marker, a ``# type: ignore`` that removes an
assertion, and mocking an assertion away all make a test pass without fixing
what the test was checking. All are refused here, on the same grounds as
deleting the file, and :data:`DELETION_SHAPES` says so in one place so a reader
can see the boundary rather than infer it. The recorded decision is in
:data:`SKIPPING_DECISION`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from runtime.turn_middleware import ToolCall, call_writes, is_read_only_command

#: Paths that name a test, in the shapes a repository actually uses. Mirrors
#: ``harness/config.py::DEFAULTS["protected_paths"]`` plus the ecosystem globs
#: ``execution/ecosystems.py`` registers; a test path this table misses is a
#: refusal this module does not make, which is the honest failure direction.
TEST_PATH_PATTERNS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "tests/*",
    "test/*",
    "spec/*",
    "*Test.java",
    "*_test.go",
    "*Tests.cs",
    "*.spec.ts",
    "*.spec.js",
    "*.test.ts",
    "*.test.js",
)

#: The closed shape vocabulary. Every refusal names exactly one, and the eight
#: shapes the brief asks for are all producible — a pin drives each one.
DELETION_SHAPES: tuple[str, ...] = (
    "rm",  # deleting a test file outright
    "git_discard",  # reverting/discarding a test edit, or removing it from the index
    "sed_in_place",  # an in-place rewrite of a test file
    "overwrite",  # `write`/`edit` replacing a test file's content
    "truncate",  # shortening a test file to nothing or nearly nothing
    "disable_marker",  # @skip / @xfail / pytest.ini marker / deselect
    "mock_assertion",  # neutralizing an assertion so it cannot fail
    "config_change",  # narrowing collection: conftest, ini, addopts
)

#: The decision this module makes about skipping, recorded rather than implied.
SKIPPING_DECISION = (
    "Skipping is deletion. A test that is marked skip, marked xfail, deselected "
    "by configuration, or made structurally unable to fail is a test that has "
    "been removed as far as the suite is concerned, and the defect it was "
    "watching for is now unguarded with a green row instead of a red one. "
    "Refused on the same grounds as deleting the file. The exception is the same "
    "one: a user may do it explicitly, and an agent may not."
)


def _is_test_path(path: str) -> bool:
    """Whether a repository-relative path names a test.

    Normalises separators and strips a leading ``./`` first, because
    ``./tests/test_x.py`` and ``tests\\test_x.py`` are the same file and a
    guard that missed either would be a guard with a hole shaped like a typo.

    The decomposition is plain string work rather than ``pathlib``: this module
    is reached from ``harness.agent_loop_step.step``, and that function's purity
    pins forbid ``pathlib`` because a module holding it could touch the
    filesystem. Splitting a normalised string cannot, and the whole reason this
    module needs no filesystem is that it judges a *proposed* call before
    anything runs.
    """
    if not path:
        return False
    cleaned = str(path).replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    if not cleaned:
        return False
    segments = [segment for segment in cleaned.split("/") if segment]
    if not segments:
        return False
    name = segments[-1]
    if name.startswith("test_") and name.endswith((".py", ".pyi")):
        return True
    if name.endswith("_test.py") or name.endswith(".test.ts") or name.endswith(".spec.ts"):
        return True
    if name.endswith((".spec.js", ".test.js")):
        return True
    if name.endswith(("Test.java", "_test.go", "Tests.cs")):
        return True
    # Left as its own branch rather than folded into the chain above: a
    # directory named `tests` and a file named `test_x.py` are different
    # evidence, and keeping them separate is what lets a pin count which rule
    # fired. The `ruff` SIM103 suggestion to merge them into one `or` would make
    # the table unreadable as data.
    return segments[0] in ("tests", "test", "spec", "__tests__")


#: Whole-file test-configuration surfaces. A write to any of these is a
#: collection-level deletion whatever the content says, because the file's only
#: job is to decide what runs.
_WHOLE_FILE_TEST_CONFIG: frozenset[str] = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "jest.config.js",
        "jest.config.ts",
        "vitest.config.ts",
        "karma.conf.js",
        "playwright.config.ts",
        "cypress.config.js",
        "phpunit.xml",
        "phpunit.xml.dist",
    }
)

#: Table-scoped surfaces. These files carry BOTH ordinary content and test
#: configuration, and R2-03/R2-12 already settled which part is protected:
#: editing ``[project] dependencies`` in ``pyproject.toml`` is a normal edit and
#: only the pytest table is the test contract. Treating the whole file as test
#: configuration would refuse every dependency bump in the repository — which is
#: the false-positive direction that gets a real guard switched off.
_TABLE_SCOPED_TEST_CONFIG: dict[str, tuple[str, ...]] = {
    "pyproject.toml": ("[tool.pytest.ini_options]", "tool.pytest.ini_options"),
    "setup.cfg": ("[tool:pytest]", "tool:pytest"),
    "tox.ini": ("[pytest]", "pytest"),
    "package.json": ('"jest"', '"scripts.test"', '"test:unit"', '"test"'),
    "angular.json": ('"test"',),
    "pom.xml": ("<test", "<surefire"),
    "build.gradle": ("test",),
    "build.gradle.kts": ("test",),
}


def _cleaned_name(path: str) -> str:
    """The final path component, with separators normalised and ``./`` stripped."""
    cleaned = str(path or "").replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rsplit("/", 1)[-1]


def _is_test_config_path(path: str) -> bool:
    """Whether a path is test *configuration* — also protected, also a deletion.

    Narrowing collection is the same act as deleting the file, and this is the
    half an agent reaches for when the obvious half is refused: a
    ``collect_ignore_glob`` in a new ``conftest.py`` removes every test without
    touching one.
    """
    cleaned = str(path or "").replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    name = cleaned.rsplit("/", 1)[-1]
    if name in _WHOLE_FILE_TEST_CONFIG:
        return True
    return name in _TABLE_SCOPED_TEST_CONFIG or name.endswith(
        (".runsettings", "test_config.yaml")
    )


def _touches_test_table(path: str, call: ToolCall) -> tuple[bool, str]:
    """Whether a write to a table-scoped file lands on its test table.

    Returns ``(touches, evidence)``. A table-scoped file whose content does not
    mention its own test section is **not** a test-configuration change, and the
    ordinary dependency edit has to keep working.
    """
    cleaned = str(path or "").replace("\\", "/").strip().rsplit("/", 1)[-1]
    markers = _TABLE_SCOPED_TEST_CONFIG.get(cleaned)
    if not markers:
        return (False, "")
    text = _text_of(call).lower()
    for marker in markers:
        if marker.lower() in text:
            return (True, marker)
    return (False, "")


# --------------------------------------------------------------------------
# The structural user/agent distinction
# --------------------------------------------------------------------------


class InitiatorUnforgeable(RuntimeError):
    """Something tried to construct a user initiator without being the user.

    Raised rather than tolerated: an agent that can forge this object has
    defeated the rule, and a forgery that is merely discouraged is a forgery
    somebody will eventually rely on.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "a user-initiated test deletion cannot be constructed from an agent "
            "context; only the caller that holds the human's own action can build "
            "one. An approval flag is not a user, and an approval-gated test "
            "deletion is a test deletion."
            + (f" ({detail})" if detail else "")
        )


@dataclass(frozen=True)
class Initiator:
    """Who asked for a tool call.

    ``user_initiated`` is derived from :data:`_USER_AUTHORITY` being the actual
    sentinel object rather than from a boolean argument, so there is no
    constructor path that produces a ``True`` except :func:`user_initiator`,
    and that function is guarded by a module-private token.
    """

    kind: str
    label: str
    #: The sentinel proves provenance. A string copy of it cannot be forged
    #: because the comparison is by identity.
    authority: object = field(repr=False, compare=False)

    @property
    def user_initiated(self) -> bool:
        return self.authority is _USER_AUTHORITY

    @property
    def agent_initiated(self) -> bool:
        return not self.user_initiated

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "user_initiated": self.user_initiated,
            "agent_initiated": self.agent_initiated,
        }


#: The authority object itself, never a copyable token. Identity is what is
#: checked, so a value that merely compares equal is refused — the difference
#: between "caller knows the secret" and "caller is the thing the module trusts".
_USER_AUTHORITY = object()
_AGENT_AUTHORITY = object()


def user_initiator(label: str, *, authority: object = None) -> Initiator:
    """Build a user initiator. Requires this module's own authority object.

    Checked by **identity** against :data:`_USER_AUTHORITY`, so an equal-looking
    stand-in does not pass and the public surface cannot hand the real one out
    (see :func:`bound_user_authority` for the surface that may). Omitting
    ``authority`` raises rather than defaulting, so the failure mode of a caller
    who did not know about it is a refusal and not a silently downgraded guard.
    """
    if authority is not _USER_AUTHORITY:
        raise InitiatorUnforgeable(
            "a user initiator requires this module's own authority object; "
            "obtain it from runtime.test_protection.bound_user_authority() on "
            "the harness side. A flag, a token value, or a stand-in object is "
            "not an authority."
        )
    return Initiator(
        kind="user", label=str(label or "the user"), authority=_USER_AUTHORITY
    )


def agent_initiator(label: str = "the agent") -> Initiator:
    """Build an agent initiator. The only kind an agent-side caller can get."""
    return Initiator(
        kind="agent", label=str(label or "the agent"), authority=_AGENT_AUTHORITY
    )


# --------------------------------------------------------------------------
# Violations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TestProtectionViolation:
    """One refused act against the test contract.

    ``evidence`` is the substring that triggered it, so a refusal is
    actionable — "a refusal a user cannot interpret is indistinguishable from
    the agent misbehaving" is the note the floor refusals carry.
    """

    shape: str
    verb: str
    path: str
    reason: str
    evidence: str = ""
    overridable: bool = False
    requires_human: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "verb": self.verb,
            "path": self.path,
            "reason": self.reason,
            "evidence": self.evidence,
            "overridable": self.overridable,
            "requires_human": self.requires_human,
            "initiator_is_user": False,
        }


@dataclass(frozen=True)
class TestProtectionRule:
    """A declared rule with its own ``match`` and ``not_match`` proofs.

    T5's Phase 2 requirement is that every rule ships its proof, and the point
    of a ``not_match`` case is that it is the only thing that distinguishes a
    rule from a substring blocklist: a rule that matched everything would pass
    every ``match`` assertion in this file.
    """

    rule_id: str
    shape: str
    statement: str
    match: tuple[Mapping[str, Any], ...]
    not_match: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "shape": self.shape,
            "statement": self.statement,
            "match": [dict(row) for row in self.match],
            "not_match": [dict(row) for row in self.not_match],
        }


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

_REMOVAL_VERBS = frozenset({"rm", "delete", "unlink", "remove", "trash", "rmdir", "del", "shred"})

#: Verbs that mutate, used only to turn a **declared corpus row** into a call.
#: It is a guess about a verb, and :func:`runtime.turn_middleware.call_writes`
#: overrides it when the call carries a shell command that can be *proven*
#: read-only — so the guess is the floor and the proof is the authority.
#:
#: Default for an unrecognised verb is **absent from this set**, which makes
#: ``call_writes`` fall through to its own default (mutating). That direction is
#: deliberate: "I do not know what this tool does" must not read as "it is safe".
_WRITING_VERBS: frozenset[str] = frozenset(
    {
        "bash",
        "shell",
        "write",
        "edit",
        "apply_patch",
        "delete",
        "rename",
        "undo",
        "memory_record",
        "test",
        "build",
        "git_commit",
        "web_fetch",
        "web_search",
        "mcp",
        "task",
    }
)
_DISCARD_MARKERS = (
    "git checkout --",
    "git restore",
    "git reset --hard",
    "git clean -fd",
    "git rm",
    "git stash drop",
)
_INPLACE_MARKERS = ("sed -i", "sed --in-place", "perl -i", "ruby -i", "truncate -s")
_DISABLE_TOKENS = (
    "pytest.mark.skip",
    "pytest.mark.skipif",
    "pytest.mark.xfail",
    "@skip",
    "@xfail",
    "unittest.skip",
    "@Ignore",
    "t.Skip",
    "t.Skip(",
    "it.skip",
    "describe.skip",
    "xdescribe",
    "collect_ignore",
    "collect_ignore_glob",
    "--deselect",
    "--ignore",
    "addopts",
    "testpaths",
)
_MOCK_ASSERTION_TOKENS = (
    "mock.patch",
    "unittest.mock",
    "MagicMock",
    "monkeypatch.setattr",
    "assert_never_called",
    "type: ignore",
    "noqa",
)


def _shell_command(text: str) -> str:
    """The command string a shell-shaped call carries, if any.

    A deletion reaches this guard through two different doors — a typed
    ``delete`` tool whose *verb* names the removal, and a ``bash`` tool whose
    *text* does. Checking only the verb field would leave the bash door open,
    which is the same "two spellings of one answer" drift this tree has already
    paid for on the redaction placeholder.
    """
    for key in ("command", "cmd", "shell", "script", "argv"):
        value = text.get(key) if isinstance(text, Mapping) else None
        if isinstance(value, str) and value.strip():
            return value
    joined = " ".join(str(part) for part in (text if isinstance(text, (list, tuple)) else ()))
    return joined.strip()


def _first_word(command: str) -> str:
    """The first shell word, minus any path and any ``VAR=value`` prefix."""
    head = command.strip().split(None, 1)
    if not head:
        return ""
    first = head[0]
    if "=" in first and not first.startswith("-") and "/" not in first:
        # An environment assignment prefix: the verb is the next word.
        rest = head[0].split("=", 1)[-1]
        tail = command.strip().split(None, 2)
        return tail[2] if len(tail) > 2 else rest
    return first.rsplit("/", 1)[-1].lower()


def _text_of(call: ToolCall) -> str:
    """Flatten a call's arguments to a searchable string.

    Arguments can arrive as a mapping, a command string, or content under any of
    the tool names this tree has used. Flattening all of them rather than
    picking one is what stops a shape from being reachable through the *other*
    tool's spelling.
    """

    def _walk(value: Any, out: list[str]) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                out.append(str(key))
                _walk(item, out)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item, out)
        elif value is not None:
            out.append(str(value))

    parts: list[str] = []
    _walk(dict(call.arguments), parts)
    return "\n".join(parts)


def _path_of(call: ToolCall) -> str:
    """The most specific path a call names.

    Checks the typed arguments first, then falls back to scanning the shell
    command's words. The fallback is not a convenience: ``rm tests/test_x.py``
    and ``truncate -s 0 tests/test_x.py`` carry their target in the command text
    and nowhere else, so a guard that only read a ``path`` field would refuse
    neither of them — the two most direct deletions in the whole shape list.
    """
    for key in ("path", "file", "target", "filename", "paths", "files", "glob"):
        value = call.arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple)) and value:
            first = str(value[0]).strip()
            if first:
                return first
    if call.path:
        return str(call.path)
    command = _shell_command(call.arguments)
    if command:
        for word in re.split(r"[\s;|&]+", command):
            candidate = word.strip("\"'")
            if not candidate or candidate.startswith("-"):
                continue
            if _is_test_path(candidate) or _is_test_config_path(candidate):
                return candidate
    return ""


def _classify(call: ToolCall) -> list[TestProtectionViolation]:
    """Every way this one call removes or disables a test.

    Eight shapes, each a distinct act. :data:`DELETION_SHAPES` is the closed
    vocabulary and every entry must be producible — an untested shape is one a
    later reader would try first.
    """
    verb = (call.verb or "").lower()
    path = _path_of(call)
    text = _text_of(call)
    lowered = text.lower()
    command = _shell_command(call.arguments)
    command_lowered = command.lower()
    found: list[TestProtectionViolation] = []

    is_test_file = _is_test_path(path)
    is_config_file = _is_test_config_path(path)
    # The ONE mutating answer. `turn_middleware.call_writes` owns it, including
    # the shell read-proof, so this guard and `read_only_gate` cannot disagree
    # about whether `git diff -- tests/` writes.
    mutates = call_writes(call) or verb in _REMOVAL_VERBS
    # A removal can be named by the typed verb OR by the first shell word. Both
    # doors reach the same act.
    shell_verb = _first_word(command_lowered)
    removing = verb in _REMOVAL_VERBS or shell_verb in _REMOVAL_VERBS

    # 1. rm
    if removing and (is_test_file or is_config_file or _mentions_test(lowered, command_lowered)):
        found.append(
            TestProtectionViolation(
                shape="rm",
                verb=verb or shell_verb,
                path=path,
                reason="deleting a test or a test configuration file",
                evidence=_first_evidence(
                    command_lowered or lowered,
                    ("test_", "tests/", "conftest.py", "pytest.ini"),
                ),
            )
        )

    # 2. git discard. One `if` with an `and`, not two nested ones: the shape is
    # "this marker appears AND this names a test", and flattening it keeps that
    # readable on one line instead of spreading the condition over two levels.
    if any(marker in lowered for marker in _DISCARD_MARKERS) and (
        is_test_file or is_config_file or _mentions_test(lowered, command_lowered)
    ):
        found.append(
            TestProtectionViolation(
                shape="git_discard",
                verb=verb,
                path=path,
                reason=(
                    "discarding or unstaging a test change removes the "
                    "test's current content"
                ),
                evidence=_first_evidence(lowered, _DISCARD_MARKERS),
            )
        )

    # 3. sed -i
    if any(marker in command_lowered for marker in _INPLACE_MARKERS[:3]) and (
        is_test_file or _mentions_test(lowered, command_lowered)
    ):
        found.append(
            TestProtectionViolation(
                shape="sed_in_place",
                verb=verb or shell_verb,
                path=path,
                reason="an in-place rewrite of a test file can remove the test",
                evidence=_first_evidence(command_lowered, _INPLACE_MARKERS[:3]),
            )
        )

    # 4. overwrite
    if is_test_file and mutates and not removing:
        found.append(
            TestProtectionViolation(
                shape="overwrite",
                verb=verb,
                path=path,
                reason="replacing a test file's content is editing a test",
                evidence=path,
            )
        )

    # 5. truncate -- two doors. The shell form names its target in the command,
    #    so `_path_of` has to look there; the write form is detected by the
    #    replacement being a fraction of the original it declares.
    if is_test_file and "truncate" in command_lowered:
        found.append(
            TestProtectionViolation(
                shape="truncate",
                verb=verb or shell_verb,
                path=path,
                reason="truncating a test file removes the test",
                evidence="truncate",
            )
        )
    if is_test_file and _looks_like_truncation(call):
        found.append(
            TestProtectionViolation(
                shape="truncate",
                verb=verb,
                path=path,
                reason="a write that replaces a test with almost nothing removes the test",
                evidence=_evidence_of_len(call),
            )
        )

    # 6. disable marker -- SKIPPING IS DELETION.
    hit = _first_evidence(lowered, _DISABLE_TOKENS)
    if hit and (is_test_file or is_config_file or _mentions_test(lowered, command_lowered)):
        found.append(
            TestProtectionViolation(
                shape="disable_marker",
                verb=verb,
                path=path,
                reason=SKIPPING_DECISION.split(". ")[0],
                evidence=hit,
            )
        )

    # 7. mock the assertion away
    if is_test_file and any(marker in lowered for marker in _MOCK_ASSERTION_TOKENS):
        found.append(
            TestProtectionViolation(
                shape="mock_assertion",
                verb=verb,
                path=path,
                reason=(
                    "neutralizing the thing a test asserts makes the test unable "
                    "to fail"
                ),
                evidence=_first_evidence(lowered, _MOCK_ASSERTION_TOKENS),
            )
        )

    # 8. configuration narrowing -- the half an agent reaches for when the
    #    obvious half is refused. `pyproject.toml` is TABLE-scoped, so only a
    #    write to its pytest table counts; a dependency bump must keep working.
    touches_table, table_evidence = _touches_test_table(path, call)
    whole_file_config = _cleaned_name(path) in _WHOLE_FILE_TEST_CONFIG
    if is_config_file and mutates and not removing and (whole_file_config or touches_table):
        found.append(
            TestProtectionViolation(
                shape="config_change",
                verb=verb,
                path=path,
                reason="narrowing test configuration is collection-level test deletion",
                evidence=path if whole_file_config else table_evidence,
            )
        )
    if _mentions_test(lowered, command_lowered) and any(
        marker in lowered
        for marker in ("collect_ignore", "collect_ignore_glob", "--deselect", "testpaths")
    ):
        found.append(
            TestProtectionViolation(
                shape="config_change",
                verb=verb,
                path=path,
                reason="narrowing test configuration is collection-level test deletion",
                evidence=_first_evidence(
                    lowered,
                    ("collect_ignore", "collect_ignore_glob", "--deselect", "testpaths"),
                ),
            )
        )

    # One act, one violation: de-duplicate by (shape, path).
    seen: set[tuple[str, str]] = set()
    unique: list[TestProtectionViolation] = []
    for violation in found:
        key = (violation.shape, violation.path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(violation)
    return unique


def _mentions_test(lowered: str, command_lowered: str = "") -> bool:
    """Whether the payload names a test at all.

    The command half counts a bare ``rm -rf build/`` as NOT naming a test, which
    is what makes the ``not_match`` arms of the rm rule meaningful: without it
    the rule would refuse ordinary cleanup, which is the false-positive direction
    that gets a real guard switched off.
    """
    haystack = f"{lowered}\n{command_lowered}".lower()
    if not haystack.strip():
        return False
    return any(
        token in haystack
        for token in (
            "test_", "tests/", "test/", "conftest.py", "pytest.ini",
            "_test.", "/spec/", "testpaths",
        )
    )


def _first_evidence(lowered: str, markers: Sequence[str]) -> str:
    for marker in markers:
        position = lowered.find(marker)
        if position != -1:
            start = max(0, position - 24)
            return lowered[start : position + len(marker) + 24].strip()
    return ""


def _looks_like_truncation(call: ToolCall) -> bool:
    """A write whose replacement is a tiny fraction of the declared original."""
    for key in ("content", "new_string", "new", "text", "body"):
        value = call.arguments.get(key)
        if not isinstance(value, str):
            continue
        declared = call.arguments.get("old_string") or call.arguments.get("original") or ""
        if not isinstance(declared, str) or not declared.strip():
            continue
        if len(value.strip()) * 4 < len(declared.strip()):
            return True
    return False


def _evidence_of_len(call: ToolCall) -> str:
    for key in ("content", "new_string", "new", "text", "body"):
        value = call.arguments.get(key)
        if isinstance(value, str):
            return f"{key}={len(value.strip())} chars against a declared original"
    return "content reduced to almost nothing"


def call_from_case(case: Mapping[str, Any]) -> ToolCall:
    """Build the :class:`ToolCall` a declared rule row describes.

    Every ``match`` and ``not_match`` row in :data:`RULES` is data, so it needs
    exactly one way to become a call — otherwise each consumer guesses, and a
    probe that guesses ``writes=True`` for a ``read`` row turns a correct guard
    into an apparent false positive (which is what the first version of this
    probe did).

    A row may state ``writes`` explicitly. Otherwise it is derived from the verb
    via :data:`_WRITING_VERBS`, and then **overridden downward** when the call
    carries a shell command that is provably read-only. That override is the
    single decision point for the question, so ``read_only_gate`` in
    ``runtime.turn_policies`` and this module cannot disagree about whether
    ``git diff -- tests/`` mutates — which is the duplication the doctrine warns
    about, avoided by having one answer rather than two careful ones.
    """
    verb = str(case.get("verb") or "")
    arguments = case.get("arguments")
    mapping = dict(arguments) if isinstance(arguments, Mapping) else {}
    writes = bool(case["writes"]) if "writes" in case else (verb.lower() in _WRITING_VERBS)
    if "writes" in case:
        return ToolCall(verb=verb, arguments=mapping, writes=writes, path=str(case.get("path") or ""))
    # A corpus row's `writes` above is a GUESS about the verb. When the call
    # carries a shell command, `turn_middleware.is_read_only_command` is the one
    # authority that can *prove* the command cannot write, and it lives where
    # `read_only_gate` also reads it — so this guard and that one cannot
    # disagree about whether `git diff -- tests/` writes.
    command = _shell_command(mapping)
    if command and is_read_only_command(command):
        writes = False
    return ToolCall(verb=verb, arguments=mapping, writes=writes, path=str(case.get("path") or ""))


def evaluate(protector: "TestProtector", calls: Iterable[ToolCall]) -> list[TestProtectionViolation]:
    """Judge proposed calls. Returns one violation per refused act."""
    violations: list[TestProtectionViolation] = []
    for call in calls or ():
        if not protector.initiator.agent_initiated:
            # The user did this. Their explicit action is not an agent action,
            # and the whole distinction exists so this branch is reachable.
            continue
        violations.extend(_classify(call))
    return violations


# --------------------------------------------------------------------------
# The protector
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TestProtector:
    """The policy object a loop binds and hands to the pipeline.

    Holds the :class:`Initiator` it was constructed with and never accepts
    another. A method that took the initiator as an argument would make the
    agent's word sufficient, which is the entire failure mode being closed.
    """

    initiator: Initiator
    allow_test_writes_for_user: bool = True

    @property
    def user_initiated(self) -> bool:
        return self.initiator.user_initiated

    def to_dict(self) -> dict[str, Any]:
        return {
            "initiator": self.initiator.to_dict(),
            "allow_test_writes_for_user": self.allow_test_writes_for_user,
            "shapes": list(DELETION_SHAPES),
            "skipping_decision": SKIPPING_DECISION,
            "approval_gated": False,
            "overridable": False,
        }


def protector_for_agent(label: str = "the agent") -> TestProtector:
    """The protector a model-driven loop uses. Refuses; has no approval branch."""
    return TestProtector(initiator=agent_initiator(label))


def protector_for_user(label: str = "the user", *, authority: object = None) -> TestProtector:
    """A user-initiated protector. Requires an AUTHORITY, not a token to copy.

    The parameter is named ``authority`` rather than ``_token`` because it is no
    longer a copyable secret: it must be the **live, already-verified
    authority object** this module issued at import. Compare by identity against
    :data:`_USER_AUTHORITY`, so a caller cannot construct an equal-looking object
    and cannot obtain the real one from this module's public surface — the
    previous ``user_token()`` let exactly that, which meant the distinction was
    one import away from gone.

    ``authority=None`` (the default) raises, so omitting it is a refusal rather
    than a silent agent-initiated protector.
    """
    return TestProtector(initiator=user_initiator(label, authority=authority))


def user_authority() -> object:
    """The live user authority, for a loop that is HONOURING a human's own action.

    This exists because deleting a test at a human's explicit request is a real
    thing that must stay possible — a guard with no user path is a guard that
    gets disabled. It is deliberately **not** re-exported in ``__all__``, and
    :func:`bound_user_authority` is the surface a loop should use instead.

    **This is not a security boundary against arbitrary in-process Python.** An
    in-process caller can read ``test_protection._USER_AUTHORITY`` or call this
    function; nothing in Python prevents that. What it does stop is the shape that
    actually happens: an agent emitting **tool calls** across a boundary that has
    no field to carry an initiator, into a loop holding an agent-built protector.
    :func:`bound_user_authority` is that boundary's trusted side, and it is the
    only path a loop needs.
    """
    return _USER_AUTHORITY


def bound_user_authority() -> object:
    """The authority a **harness-side** caller passes to :func:`protector_for_user`.

    Named to say what it is: a privilege this module grants to code that is
    running the loop on a human's behalf, obtained by *importing* rather than by
    presenting a value. An agent cannot import; it emits tool calls. That is the
    whole distinction, and it is why this exists rather than a token parameter:
    a token is a value a caller can be handed, and handing it around is how it
    stops meaning anything.
    """
    return _USER_AUTHORITY


# --------------------------------------------------------------------------
# The declared rule set, with each rule's own proof
# --------------------------------------------------------------------------


def _rule(
    rule_id: str,
    shape: str,
    statement: str,
    match: Sequence[Mapping[str, Any]],
    not_match: Sequence[Mapping[str, Any]],
) -> TestProtectionRule:
    return TestProtectionRule(
        rule_id=rule_id,
        shape=shape,
        statement=statement,
        match=tuple(match),
        not_match=tuple(not_match),
    )


RULES: tuple[TestProtectionRule, ...] = (
    _rule(
        "test_removal.rm",
        "rm",
        "The agent may not delete a test file or a test configuration file.",
        match=(
            {"verb": "bash", "arguments": {"command": "rm tests/test_cart.py"}},
            {"verb": "bash", "arguments": {"command": "rm -f test_*.py"}},
            {"verb": "delete", "arguments": {"path": "tests/test_cart.py"}},
        ),
        not_match=(
            {"verb": "bash", "arguments": {"command": "rm -rf build/"}},
            {"verb": "bash", "arguments": {"command": "rm docs/notes.md"}},
        ),
    ),
    _rule(
        "test_removal.git_discard",
        "git_discard",
        "The agent may not discard, revert or unstage a test change.",
        match=(
            {"verb": "bash", "arguments": {"command": "git checkout -- tests/"}},
            {"verb": "bash", "arguments": {"command": "git restore tests/test_cart.py"}},
            {"verb": "bash", "arguments": {"command": "git rm tests/test_cart.py"}},
        ),
        not_match=(
            {"verb": "bash", "arguments": {"command": "git checkout -- src/cart.py"}},
            {"verb": "bash", "arguments": {"command": "git diff -- tests/"}},
        ),
    ),
    _rule(
        "test_removal.sed_in_place",
        "sed_in_place",
        "The agent may not rewrite a test file in place.",
        match=(
            {"verb": "bash", "arguments": {"command": "sed -i 's/assert/print/' tests/test_cart.py"}},
            {"verb": "bash", "arguments": {"command": "perl -i -pe 's/\\btest\\b//g' test_thing.py"}},
        ),
        not_match=(
            {"verb": "bash", "arguments": {"command": "sed -i 's/foo/bar/' src/cart.py"}},
            {"verb": "bash", "arguments": {"command": "sed -n '1,20p' tests/test_cart.py"}},
        ),
    ),
    _rule(
        "test_removal.overwrite",
        "overwrite",
        "The agent may not replace a test file's content.",
        match=(
            {"verb": "write", "arguments": {"path": "tests/test_cart.py", "content": "def test_x():\n    assert True\n"}},
            {"verb": "edit", "arguments": {"path": "test_thing.py", "old_string": "assert total == 5", "new_string": "assert total == 6"}},
        ),
        not_match=(
            {"verb": "write", "arguments": {"path": "src/cart.py", "content": "def total():\n    return 5\n"}},
            {"verb": "read", "arguments": {"path": "tests/test_cart.py"}},
        ),
    ),
    _rule(
        "test_removal.truncate",
        "truncate",
        "The agent may not truncate a test file.",
        match=(
            {"verb": "bash", "arguments": {"command": "truncate -s 0 tests/test_cart.py"}},
            {
                "verb": "write",
                "arguments": {
                    "path": "tests/test_cart.py",
                    "content": "x",
                    "old_string": "def test_add():\n    assert cart.add(1, 2) == 3\n    assert cart.add(-1, 1) == 0\n",
                },
            },
        ),
        not_match=(
            {"verb": "bash", "arguments": {"command": "truncate -s 0 src/cart.py"}},
            {"verb": "bash", "arguments": {"command": "wc -l tests/test_cart.py"}},
        ),
    ),
    _rule(
        "test_removal.disable_marker",
        "disable_marker",
        "Skipping is deletion: @skip, @xfail, a deselect and a pytest.ini marker "
        "are refused on the same grounds as deleting the file.",
        match=(
            {"verb": "edit", "arguments": {"path": "tests/test_cart.py", "old_string": "def test_add():", "new_string": "@pytest.mark.skip\ndef test_add():"}},
            {"verb": "write", "arguments": {"path": "pytest.ini", "content": "[pytest]\naddopts = --deselect tests/test_cart.py\n"}},
            {"verb": "bash", "arguments": {"command": "sed -i '1i @pytest.mark.xfail' tests/test_cart.py"}},
        ),
        not_match=(
            {"verb": "bash", "arguments": {"command": "python -m pytest tests/test_cart.py -q"}},
            {"verb": "edit", "arguments": {"path": "src/cart.py", "old_string": "def total():", "new_string": "@functools.cache\ndef total():"}},
        ),
    ),
    _rule(
        "test_removal.mock_assertion",
        "mock_assertion",
        "The agent may not neutralize a test's assertion so it cannot fail.",
        match=(
            {"verb": "edit", "arguments": {"path": "tests/test_cart.py", "old_string": "assert cart.total() == 5", "new_string": "pass  # type: ignore"}},
            {"verb": "edit", "arguments": {"path": "tests/test_cart.py", "old_string": "assert cart.total() == 5", "new_string": "cart.total = MagicMock(return_value=5)\nassert True"}},
        ),
        not_match=(
            {"verb": "edit", "arguments": {"path": "src/cart.py", "old_string": "cart.total()", "new_string": "cart.subtotal()"}},
        ),
    ),
    _rule(
        "test_removal.config_change",
        "config_change",
        "Narrowing test configuration is collection-level deletion, and is "
        "refused even when no test file is named.",
        match=(
            {"verb": "write", "arguments": {"path": "conftest.py", "content": "collect_ignore_glob = ['test_cart.py']\n"}},
            {"verb": "write", "arguments": {"path": "pytest.ini", "content": "[pytest]\ntestpaths = tests/broken\n"}},
            {"verb": "bash", "arguments": {"command": "python -m pytest --ignore=tests/test_cart.py"}},
        ),
        not_match=(
            {"verb": "write", "arguments": {"path": "pyproject.toml", "content": "[project]\nname = 'x'\n"}},
            {"verb": "bash", "arguments": {"command": "python -m pytest --collect-only tests/"}},
        ),
    ),
)


def rule_table() -> list[dict[str, Any]]:
    """Every declared rule with its ``match`` and ``not_match`` proofs."""
    return [rule.to_dict() for rule in RULES]


def shapes_table() -> list[dict[str, Any]]:
    """The closed shape vocabulary with its rule id."""
    return [
        {
            "shape": shape,
            "rule_id": next((r.rule_id for r in RULES if r.shape == shape), ""),
            "in_rules": any(r.shape == shape for r in RULES),
        }
        for shape in DELETION_SHAPES
    ]


__all__ = [
    "DELETION_SHAPES",
    "RULES",
    "SKIPPING_DECISION",
    "TEST_PATH_PATTERNS",
    "Initiator",
    "InitiatorUnforgeable",
    "TestProtectionRule",
    "TestProtectionViolation",
    "TestProtector",
    "agent_initiator",
    "bound_user_authority",
    "call_from_case",
    "evaluate",
    "protector_for_agent",
    "protector_for_user",
    "rule_table",
    "shapes_table",
    "user_authority",
    "user_initiator",
]
