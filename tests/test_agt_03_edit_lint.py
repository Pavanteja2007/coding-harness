"""AGT-03 â€” the lint check runs INSIDE the edit, before the write is committed.

SWE-agent measured **-15.0%** without an in-edit guardrail: the loop's lint
gate runs *after* an edit, so a broken edit exists in the work tree and is
then rolled back. This suite pins the mechanism that refuses the mutation at
the tool boundary, and pins the three things that are easy to get wrong about
it:

  1. a syntax-breaking edit is REFUSED and the file is byte-identical to
     before â€” discarded, never applied-then-undone;
  2. the refusal carries Â±3 lines of context, because "syntax error at line
     2" is not a form a model can self-correct from;
  3. a file with no checker for it is reported as UNCHECKED and never as
     passed â€” a silent skip reads as a pass;
  4. every mutating tool is screened, not just `edit`;
  5. the loop's own post-edit lint gate is still there as the backstop, and
     this suite fails if it is ever removed.

Host-only: no Docker, no model, no network. The gate itself is stdlib
(`compile()` for Python, tree-sitter for JS/TS), and the editor's refusals are
the production ones.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from execution.workspace import SafeToolBackend, Workspace  # noqa: E402
from harness import editor, lint, tools  # noqa: E402

VALID = b"def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def write_bytes(root: Path, rel: str, data: bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def read_session(root: Path, rel: str) -> editor.EditSession:
    """An `EditSession` that has legitimately read `rel` (the digest ledger)."""
    session = editor.EditSession()
    session.note_read(rel, data=(root / rel).read_bytes())
    return session


class RecordingBackend:
    """Minimal stand-in for `SafeToolBackend` that records what it was asked.

    Its only job is to prove the gate runs BEFORE the backend: a refused call
    must never appear in `calls`, and an accepted one must be forwarded
    verbatim."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.calls: list[tuple[str, dict]] = []

    def execute(self, tool: str, args=None, *, sandboxed: bool = False):
        self.calls.append((tool, dict(args or {})))
        return {"ok": True, "tool": tool, "wrote": True}


# ---------------------------------------------------------------------------
# 1. the pre-commit refusal: refused, byte-identical, never applied-then-undone
# ---------------------------------------------------------------------------


def test_a_syntax_breaking_edit_is_refused_and_the_file_is_byte_identical(
    tmp_path: Path,
) -> None:
    target = write_bytes(tmp_path, "calc.py", VALID)
    session = read_session(tmp_path, "calc.py")

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "calc.py",
        "def add(a, b):\n    return a + b\n",
        "def add(a, b)\n    return a + b(\n",
        session=session,
    )

    assert outcome.ok is False
    assert outcome.error_kind == editor.ERROR_POST_CHECK_FAILED
    # THE load-bearing assertion: nothing was written, so there is nothing to
    # undo. `pre_commit` is what distinguishes "discarded" from "rolled back".
    assert outcome.pre_commit is True
    assert target.read_bytes() == VALID
    assert outcome.write_mode == "pre_commit_refused"
    # `rolled_back` keeps its documented meaning - the file on disk is
    # byte-identical to its pre-edit state - and the mechanism is named
    # separately so the receipt cannot be read as "a write happened".
    assert outcome.rolled_back is True
    assert "rolled back: no change was ever committed" in outcome.message
    assert "Nothing was written" in outcome.message


def test_the_refusal_is_never_reported_as_a_syntax_free_pass(tmp_path: Path) -> None:
    write_bytes(tmp_path, "calc.py", VALID)
    outcome = editor.apply_text_edit(
        str(tmp_path),
        "calc.py",
        "    return a + b\n",
        "    return a + b(\n",
        session=read_session(tmp_path, "calc.py"),
    )
    assert outcome.check_status == lint.CHECK_FAILED
    assert outcome.check_status in lint.CHECK_STATUSES
    assert "syntax error" in outcome.message
    assert outcome.check_line > 0


def test_the_refusal_is_traced_with_the_check_receipt(tmp_path: Path) -> None:
    class Sink:
        def __init__(self) -> None:
            self.rows: list[tuple[str, dict]] = []

        def log(self, kind, data):
            self.rows.append((kind, data))

    sink = Sink()
    write_bytes(tmp_path, "calc.py", VALID)
    editor.apply_text_edit(
        str(tmp_path),
        "calc.py",
        "    return a + b\n",
        "    return a + b(\n",
        session=read_session(tmp_path, "calc.py"),
        trace=sink,
    )
    kinds = [kind for kind, _ in sink.rows]
    assert "edit_rolled_back" in kinds
    row = dict(sink.rows)["edit_rolled_back"]
    assert row["pre_commit"] is True
    assert row["check_status"] == lint.CHECK_FAILED
    # The receipt is JSON-safe: a journal that cannot be re-read is not a
    # receipt.
    assert json.loads(json.dumps(row))["check_context"]


# ---------------------------------------------------------------------------
# 2. Â±3 lines of context - the form that actually helps
# ---------------------------------------------------------------------------


def test_the_refusal_carries_three_lines_of_context_either_side(tmp_path: Path) -> None:
    body = "\n".join(f"line_{i} = {i}" for i in range(1, 21)).encode() + b"\n"
    target = write_bytes(tmp_path, "long.py", body)
    # Break line 10 only.
    broken = body.replace(b"line_10 = 10", b"line_10 = (10")
    assert broken != body
    target.write_bytes(body)

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "long.py",
        "line_10 = 10",
        "line_10 = (10",
        session=read_session(tmp_path, "long.py"),
    )

    assert outcome.ok is False
    assert outcome.check_line == 10
    context = outcome.check_context
    shown = [
        int(line.split("|")[0].strip().lstrip("> ")) for line in context.splitlines()
    ]
    assert shown == [7, 8, 9, 10, 11, 12, 13]
    assert "line_10 = (10" in context
    # The offending line is marked, so the model knows which of the seven is
    # the problem rather than guessing.
    assert any(line.startswith(">") for line in context.splitlines())
    assert context in outcome.message
    # And the file is still the pre-image.
    assert target.read_bytes() == body


def test_the_context_block_is_capped_so_a_refusal_cannot_become_a_payload() -> None:
    # (a) one enormous line is truncated from the RIGHT, because the left edge
    #     is what a reader needs to recognise the line.
    one_huge = "x = '" + "a" * 5000 + "\n"
    check = lint.check_source_for_edit("def f(:\n" + one_huge, "huge.py")
    assert check.status == lint.CHECK_FAILED
    assert check.context.endswith("...")
    assert "a" * 1000 not in check.context

    # (b) seven long lines can still exceed the block cap, and then the block
    #     says so rather than silently clipping.
    many = "".join(f"v{i} = '{'b' * 500}'\n" for i in range(1, 21))
    block = lint.check_source_for_edit(many.replace("v10 =", "v10 = ("), "many.py")
    assert block.status == lint.CHECK_FAILED
    assert len(block.context) <= lint.MAX_CONTEXT_CHARS + 64
    assert "context truncated" in block.context


# ---------------------------------------------------------------------------
# 3. a valid edit passes, and says so
# ---------------------------------------------------------------------------


def test_a_valid_edit_is_applied_and_reports_the_check_as_passed(
    tmp_path: Path,
) -> None:
    target = write_bytes(tmp_path, "calc.py", VALID)
    outcome = editor.apply_text_edit(
        str(tmp_path),
        "calc.py",
        "return a + b\n",
        "return a + b * 2\n",
        session=read_session(tmp_path, "calc.py"),
    )
    assert outcome.ok is True
    assert outcome.check_status == lint.CHECK_PASSED
    assert outcome.pre_commit is True
    assert b"a + b * 2" in target.read_bytes()
    # A clean check adds no noise to the success message.
    assert "in-edit lint" not in outcome.message
    assert outcome.to_dict()["check_status"] == lint.CHECK_PASSED


# ---------------------------------------------------------------------------
# 4. uncheckable is NOT passed - the honesty requirement
# ---------------------------------------------------------------------------


def test_a_file_with_no_checker_is_reported_as_unchecked_not_passed(
    tmp_path: Path,
) -> None:
    target = write_bytes(tmp_path, "notes.md", b"prose, not source\n")
    outcome = editor.apply_text_edit(
        str(tmp_path),
        "notes.md",
        "prose",
        "more prose",
        session=read_session(tmp_path, "notes.md"),
    )
    assert outcome.ok is True  # the edit is legitimate; there is no checker
    assert outcome.check_status == lint.CHECK_UNCHECKED
    assert outcome.check_status != lint.CHECK_PASSED
    # Requirement: the tool result SAYS it was not checked. A silent skip
    # reads as a pass.
    assert "unchecked" in outcome.message
    assert "in-edit lint" in outcome.message
    assert "no in-edit syntax checker" in outcome.check_reason
    assert b"more prose" in target.read_bytes()


def test_the_three_statuses_that_are_not_passes_all_carry_a_reason() -> None:
    for status in (lint.CHECK_UNCHECKED, lint.CHECK_DISABLED):
        check = lint.EditCheck(status, "x.py", reason="because")
        rendered = lint.render_check(check)
        assert status in rendered
        assert "because" in rendered
        assert check.checked is False
        assert check.ok is True
    assert lint.render_check(lint.EditCheck(lint.CHECK_PASSED, "x.py")) == ""


def test_a_check_that_cannot_run_is_never_a_pass(tmp_path: Path) -> None:
    # `.txt` and `.md` have no checker; a NUL byte is a failure, not a skip.
    assert lint.check_source_for_edit("hello", "a.txt").status == lint.CHECK_UNCHECKED
    assert lint.check_source_for_edit("hello", "a.md").status == lint.CHECK_UNCHECKED
    assert lint.check_source_for_edit("x = 1\x00\n", "a.py").status == lint.CHECK_FAILED
    assert lint.check_source_for_edit(None, "a.py").status == lint.CHECK_UNCHECKED  # type: ignore[arg-type]
    assert lint.check_source_for_edit("x = 1\n", "").status == lint.CHECK_UNCHECKED


def test_the_gate_can_be_turned_off_and_says_so(tmp_path: Path) -> None:
    write_bytes(tmp_path, "calc.py", VALID)
    outcome = editor.apply_text_edit(
        str(tmp_path),
        "calc.py",
        "    return a + b\n",
        "    return a + b(\n",
        session=read_session(tmp_path, "calc.py"),
        config={"edit_inline_lint": False},
    )
    # The escape hatch exists and is honest: the broken content IS written and
    # the receipt says no check ran. This is the OFF arm, not the default.
    assert outcome.ok is True
    assert outcome.check_status == lint.CHECK_DISABLED
    assert "in-edit lint: disabled" in outcome.message
    assert "edit_inline_lint" in outcome.check_reason


def test_only_an_explicit_false_disables_the_gate(tmp_path: Path) -> None:
    write_bytes(tmp_path, "calc.py", VALID)
    for config in ({}, {"edit_inline_lint": None}, {"edit_inline_lint": True}):
        outcome = editor.apply_text_edit(
            str(tmp_path),
            "calc.py",
            "    return a + b\n",
            "    return a + b(\n",
            session=read_session(tmp_path, "calc.py"),
            config=config,
        )
        assert outcome.ok is False, config
        assert outcome.check_status == lint.CHECK_FAILED, config


def test_no_in_edit_lint_key_is_in_the_harness_defaults() -> None:
    """A default in `DEFAULTS` is merged into EVERY task and eval arm.

    The R2-07 / R2-03 precedent: the keys live in the module with bounded
    internal defaults, and this test fails the moment someone publishes one
    with a real value, because a default here silently switches every run."""
    from harness.config import DEFAULTS

    for key in DEFAULTS:
        assert not key.startswith("edit_inline_lint"), key


# ---------------------------------------------------------------------------
# 5. every mutating tool is screened
# ---------------------------------------------------------------------------


def test_the_gate_covers_every_mutating_tool_in_the_catalog() -> None:
    catalog = set(tools.canonical_tool_names())
    for name in ("edit", "write", "apply_patch", "rename", "delete", "undo"):
        assert name in catalog, name
        assert name in tools.MUTATING_TOOLS, name
    for name in ("rename_symbol", "update_signature"):
        assert name in catalog, name
        assert name in tools.MUTATING_TOOLS, name
    # A read-only tool is NOT a mutation and must not be screened.
    for name in ("read", "grep", "glob", "list", "find_references"):
        assert name not in tools.MUTATING_TOOLS, name


def test_a_broken_write_is_refused_before_the_backend_is_reached(
    tmp_path: Path,
) -> None:
    write_bytes(tmp_path, "calc.py", VALID)
    backend = RecordingBackend(str(tmp_path))
    runtime = tools.TypedToolRuntime(backend, config={"require_edit_digest": False})

    result = runtime.execute("write", {"path": "calc.py", "content": "def f(:\n"})

    assert backend.calls == []  # the backend never saw it
    assert result.ok is False
    assert "refused before it was written" in (result.error or "")
    assert (tmp_path / "calc.py").read_bytes() == VALID


def test_a_good_write_reaches_the_backend_verbatim(tmp_path: Path) -> None:
    write_bytes(tmp_path, "calc.py", VALID)
    backend = RecordingBackend(str(tmp_path))
    runtime = tools.TypedToolRuntime(backend)
    runtime.execute("write", {"path": "calc.py", "content": "value = 1\n"})
    assert backend.calls == [("write", {"path": "calc.py", "content": "value = 1\n"})]


def test_a_read_only_call_skips_the_gate_entirely(tmp_path: Path) -> None:
    backend = RecordingBackend(str(tmp_path))
    receipt = tools.precommit_check_call(str(tmp_path), "read", {"path": "calc.py"})
    assert receipt["status"] == tools.GATE_NOT_APPLICABLE
    assert receipt["checked"] is False
    tools.TypedToolRuntime(backend).execute("read", {"path": "calc.py"})
    assert backend.calls == [("read", {"path": "calc.py"})]


def test_every_mutating_tool_reports_a_status_from_the_closed_vocabulary(
    tmp_path: Path,
) -> None:
    write_bytes(tmp_path, "calc.py", VALID)
    allowed = set(tools.gate_statuses())
    cases = [
        ("edit", {"path": "calc.py", "old_string": "a + b", "new_string": "a - b"}),
        ("edit", {"path": "calc.py", "old_string": "a + b", "new_string": "a + b("}),
        ("write", {"path": "calc.py", "content": "x = 1\n"}),
        ("write", {"path": "notes.md", "content": "prose\n"}),
        ("rename", {"source_path": "calc.py", "destination_path": "lib/calc.py"}),
        ("delete", {"path": "calc.py"}),
        ("undo", {}),
        ("rename_symbol", {"path": "calc.py", "old_string": "a", "new_string": "b"}),
    ]
    for name, args in cases:
        receipt = tools.precommit_check_call(str(tmp_path), name, args)
        assert receipt["status"] in allowed, (name, receipt)
        # JSON-safe, and a reader can always tell what happened.
        assert json.loads(json.dumps(receipt))["status"] == receipt["status"]


def test_a_multi_file_patch_is_refused_when_any_one_file_would_break(
    tmp_path: Path,
) -> None:
    write_bytes(tmp_path, "a.py", VALID)
    write_bytes(tmp_path, "b.md", b"prose\n")
    patch = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
        "-    return a + b\n+    return a + b(\n"
        "--- a/b.md\n+++ b/b.md\n@@ -1,1 +1,1 @@\n-prose\n+prose2\n"
    )
    receipt = tools.precommit_check_call(
        str(tmp_path), "apply_patch", {"patch": patch, "expected_revisions": {}}
    )
    assert receipt["refused"] is True
    assert receipt["status"] == lint.CHECK_FAILED
    # Per-file coverage is visible rather than assumed, and the broken file
    # cannot hide behind the unchecked one.
    statuses = {row["path"]: row["status"] for row in receipt["candidates"]}
    assert statuses["a.py"] == lint.CHECK_FAILED
    assert statuses["b.md"] == lint.CHECK_UNCHECKED


def test_a_clean_multi_file_patch_passes_with_the_weakest_status_reported(
    tmp_path: Path,
) -> None:
    write_bytes(tmp_path, "a.py", VALID)
    write_bytes(tmp_path, "b.py", VALID)
    patch = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
        "-    return a + b\n+    return a - b\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
        "-    return a + b\n+    return a + b * 3\n"
    )
    receipt = tools.precommit_check_call(
        str(tmp_path), "apply_patch", {"patch": patch, "expected_revisions": {}}
    )
    assert receipt["status"] == lint.CHECK_PASSED
    assert receipt["refused"] is False
    assert len(receipt["candidates"]) == 2


def test_a_patch_that_cannot_be_reconstructed_is_never_reported_as_a_pass(
    tmp_path: Path,
) -> None:
    write_bytes(tmp_path, "a.py", VALID)
    # Context that does not match the file: the gate cannot know what the
    # result would be, so it must say so rather than assume it is fine.
    patch = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n nothing like this\n-nor this\n+replacement\n"
    receipt = tools.precommit_check_call(
        str(tmp_path), "apply_patch", {"patch": patch, "expected_revisions": {}}
    )
    assert receipt["refused"] is False
    assert receipt["checked"] is False
    assert receipt["status"] == tools.GATE_NOT_APPLICABLE
    assert "could not be applied" in receipt["candidates"][0]["reason"]


def test_a_rename_screens_the_content_under_its_destination_name(
    tmp_path: Path,
) -> None:
    write_bytes(tmp_path, "calc.py", VALID)
    receipt = tools.precommit_check_call(
        tmp_path and str(tmp_path),
        "rename",
        {"source_path": "calc.py", "destination_path": "lib/notes.md"},
    )
    assert receipt["refused"] is False
    # The DESTINATION's extension selects the language, so a file that arrived
    # broken is reported as unchecked rather than quietly clean.
    assert receipt["status"] == lint.CHECK_UNCHECKED
    assert receipt["path"] == "lib/notes.md"


def test_a_gate_that_cannot_read_the_file_never_claims_a_pass(tmp_path: Path) -> None:
    receipt = tools.precommit_check_call(
        str(tmp_path),
        "edit",
        {"path": "missing.py", "old_string": "a", "new_string": "b"},
    )
    assert receipt["status"] == tools.GATE_NOT_APPLICABLE
    assert receipt["checked"] is False
    assert "does not exist yet" in receipt["reason"]


def test_the_facade_resolves_a_real_backend_root_and_refuses_before_the_write(
    tmp_path: Path,
) -> None:
    """The one-call hook the backend owner needs, proved against the REAL
    `SafeToolBackend` - including the root attribute it actually exposes
    (`workspace.root`, not `root`), which is why
    `harness.tools.backend_repo_root` exists."""
    workspace = Workspace(str(tmp_path))
    backend = SafeToolBackend(workspace)
    assert tools.backend_repo_root(backend) == str(workspace.root)

    before = (tmp_path / "calc.py").write_bytes(VALID)
    revision = workspace.revision("calc.py")
    result = backend.execute(
        "write",
        {
            "path": "calc.py",
            "content": "def add(a, b)\n    return a + b(\n",
            "expected_revision": revision.sha256 if revision else "",
            "allow_preexisting_change": True,
        },
    )
    # The backend's own post-write protection still holds today...
    assert result.ok is False
    assert (tmp_path / "calc.py").read_bytes() == VALID

    # ...and the gate is what refuses it BEFORE the write once wired. This is
    # the assertion the wiring has to keep satisfying.
    gate = tools.precommit_check_call(
        tools.backend_repo_root(backend),
        "write",
        {"path": "calc.py", "content": "def add(a, b)\n    return a + b(\n"},
    )
    assert gate["refused"] is True
    assert gate["status"] == lint.CHECK_FAILED
    assert (tmp_path / "calc.py").read_bytes() == VALID
    assert before == len(VALID)


def test_a_write_is_still_checked_when_no_root_can_be_resolved(tmp_path: Path) -> None:
    # The candidate content of a `write` IS the argument, so the only thing a
    # missing root costs is the overwrite pre-check. The refusal must still
    # happen - degrading to a silent pass would be the one wrong answer.
    receipt = tools.precommit_check_call(
        None, "write", {"path": "calc.py", "content": "def f(:\n"}
    )
    assert receipt["refused"] is True
    assert receipt["status"] == lint.CHECK_FAILED
    # An `edit` genuinely cannot be checked without the pre-image, and says so.
    edit_receipt = tools.precommit_check_call(
        None, "edit", {"path": "calc.py", "old_string": "a", "new_string": "b("}
    )
    assert edit_receipt["checked"] is False
    assert "no repository root" in edit_receipt["reason"]


def test_a_non_utf8_file_is_not_decoded_for_checking(tmp_path: Path) -> None:
    # latin-1 bytes that are NOT valid UTF-8. Reading them with
    # errors="replace" would check mojibake and could refuse a file that is
    # fine, so the gate reports "not decoded" instead of guessing.
    target = write_bytes(tmp_path, "latin.py", b"x = '\xe9'\n")
    with pytest.raises(UnicodeDecodeError):
        target.read_bytes().decode("utf-8", errors="strict")
    receipt = tools.precommit_check_call(
        str(tmp_path), "rename", {"source_path": "latin.py", "destination_path": "x.py"}
    )
    assert receipt["checked"] is False
    assert "not valid UTF-8" in receipt["reason"]


# ---------------------------------------------------------------------------
# 6. the loop gate is still the backstop (requirement: keep BOTH layers)
# ---------------------------------------------------------------------------


def test_the_loop_gate_still_fires_for_an_edit_that_slipped_through(
    tmp_path: Path,
) -> None:
    """The in-edit guard is a guard; `check_edits` is the audit.

    An edit made while the in-edit check is off (a config-pin, a tool that
    does not go through the editor, a shell redirect) must still be caught by
    the pre-verify loop gate, which sees every changed file rather than one
    call's arguments."""
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    write_bytes(pristine, "calc.py", VALID)
    write_bytes(work, "calc.py", VALID)
    # Write the broken content DIRECTLY, exactly as a shell redirect would.
    (work / "calc.py").write_text(
        "def add(a, b)\n    return a + b(\n", encoding="utf-8"
    )

    ok, message, changed = editor.check_edits(str(pristine), str(work), [])

    assert ok is False
    assert "syntax error" in message
    assert changed == ["calc.py"]


def test_the_loop_gate_still_ignores_files_it_cannot_check(tmp_path: Path) -> None:
    pristine = tmp_path / "pristine"
    work = tmp_path / "work"
    write_bytes(pristine, "notes.txt", b"prose\n")
    write_bytes(work, "notes.txt", b"more prose\n")
    ok, message, changed = editor.check_edits(str(pristine), str(work), [])
    assert ok is True, message
    assert changed == ["notes.txt"]


def test_the_loop_gates_lint_module_is_still_wired_into_the_run_loop() -> None:
    """Reads `harness/core.py` and FAILS if the loop's lint gate goes away.

    AGT-03 adds a layer in front of the gate; it must not become a reason to
    delete the gate. This is the pin that turns that into an ACTIVE check
    rather than a claim in a docstring."""
    source = (ROOT / "harness" / "core.py").read_text(encoding="utf-8")
    assert "lint_changed" in source, "the loop's pre-verify lint gate is gone"
    assert 'cfg.get("lint_gate"' in source, "the lint_gate config key is no longer read"
    assert "lint_failed" in source, "the lint_failed trace event is gone"
    # And the module-level gate is still a second, separate wiring.
    assert "lint_mod.lint_changed" in source


def test_the_loop_gate_still_finds_a_broken_file_through_lint_changed(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    write_bytes(work, "calc.py", b"def add(a, b)\n    return a + b(\n")
    findings = lint.lint_changed(str(work), ["calc.py"])
    assert findings
    assert findings[0].kind == "syntax"
    assert "LINT FAILED" in lint.render_findings(findings)
    # ...and a non-source file is not a finding, which is the loop gate's
    # documented bias: it must not invent a syntax error in a text file.
    write_bytes(work, "notes.txt", b"just some prose\n")
    assert lint.lint_changed(str(work), ["notes.txt"]) == []


# ---------------------------------------------------------------------------
# 7. the primitive itself: language-aware, tri-state, cheap
# ---------------------------------------------------------------------------


def test_the_check_is_language_aware() -> None:
    assert lint.language_of("a.py") == "python"
    assert lint.language_of("a.pyi") == "python"
    assert lint.language_of("a.tsx") == "typescript"
    assert lint.language_of("src\\deep\\a.js") == "javascript"
    assert lint.language_of("README.md") == ""
    assert lint.check_source_for_edit("x = 1\n", "a.py").status == lint.CHECK_PASSED
    # A JS file is checked with the JS grammar, not with compile(). These two
    # are the discriminator: `const` is a JS declaration and a Python
    # syntax error, so the path - not the parser - picks the verdict.
    assert (
        lint.check_source_for_edit("const a = 1;\n", "a.js").status == lint.CHECK_PASSED
    )
    assert (
        lint.check_source_for_edit("const a = 1;\n", "a.py").status == lint.CHECK_FAILED
    )


def test_a_missing_grammar_is_reported_not_assumed() -> None:
    """A host without the tree-sitter grammar must not read as a pass."""
    reason = lint.checker_unavailable_reason("python")
    assert reason == ""  # compile() is always available
    for language in ("javascript", "typescript"):
        # Whatever this host has, the answer is a DECISION with a reason, not
        # a default. On a host with the grammar the reason is empty.
        assert isinstance(lint.checker_unavailable_reason(language), str)
    assert "is registered for cobol" in lint.checker_unavailable_reason("cobol")


def test_the_undefined_name_pass_is_opt_in_and_off_by_default() -> None:
    source = "print(MISSING_NAME)\n"
    assert lint.check_source_for_edit(source, "a.py").status == lint.CHECK_PASSED
    strict = lint.check_source_for_edit(source, "a.py", check_names=True)
    assert strict.status == lint.CHECK_FAILED
    assert strict.kind == "undefined_name"
    assert strict.context  # Â±3 lines ride a name failure too


def test_the_editor_check_is_config_driven_and_never_raises(tmp_path: Path) -> None:
    assert editor.precommit_check("x = 1\n", "a.py").status == lint.CHECK_PASSED
    assert (
        editor.precommit_check(
            "x = 1\n", "a.py", config={"edit_inline_lint": False}
        ).status
        == lint.CHECK_DISABLED
    )
    # A crashing checker is a FAILED check, never a pass.
    import harness.lint as lint_mod

    original = lint_mod.check_source_for_edit
    try:
        lint_mod.check_source_for_edit = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        result = editor.precommit_check("x = 1\n", "a.py")
    finally:
        lint_mod.check_source_for_edit = original
    assert result.status == lint.CHECK_FAILED
    assert "boom" in result.message


def test_a_bad_context_span_degrades_instead_of_raising() -> None:
    for span in ("three", None, -1, 0):
        check = lint.check_source_for_edit("def f(:\n", "a.py", context_lines=span)  # type: ignore[arg-type]
        assert check.status == lint.CHECK_FAILED


# ---------------------------------------------------------------------------
# 8. honesty pins: what is NOT covered yet
# ---------------------------------------------------------------------------


def test_the_codemod_path_does_not_yet_use_the_in_edit_gate() -> None:
    """SELF-ARMING PIN. This is the honest state of requirement 4's list.

    `harness/codemod.py::apply_plan` dispatches each replacement through
    `backend.execute("edit", ...)` on a `SafeToolBackend`, so a codemod edit is
    refused by that backend's own post-write rollback rather than by this
    gate. That still leaves nothing broken in the tree - which is why this is a
    coverage gap and not a defect - but it is NOT the pre-commit refusal the
    brief asks for, and the backend's owner has to wire
    `harness.tools.precommit_check_call` for it to become one.

    When that wiring lands this test FAILS, which is the intent: it becomes
    the active check that the codemod path really is screened.
    """
    source = (ROOT / "harness" / "codemod.py").read_text(encoding="utf-8")
    assert "precommit" not in source, (
        "harness/codemod.py now references the in-edit gate - update this pin "
        "to assert that a codemod edit is refused BEFORE the write instead"
    )


def test_the_workspace_backend_owner_has_a_one_call_hook_to_wire() -> None:
    """The gate is reachable from `SafeToolBackend` without a new concept."""
    assert callable(tools.precommit_check_call)
    assert callable(tools.precommit_candidates)
    # The workspace backend is NOT wired yet, and this says so in code.
    source = (ROOT / "execution" / "workspace.py").read_text(encoding="utf-8")
    assert "precommit" not in source, (
        "execution/workspace.py now screens mutations - re-run the AGT-03 "
        "suite and update harness/AGENTS.md"
    )


# ---------------------------------------------------------------------------
# 9. the verifier gate is not this round's to weaken
# ---------------------------------------------------------------------------


def test_nothing_in_this_round_touches_the_completion_or_verify_path() -> None:
    """The in-edit check refuses MUTATIONS. It must not have any say in
    whether a run completed, and no module that mints completion may be
    importing it."""
    for rel in (
        "harness/core.py",
        "harness/agent_loop.py",
        "harness/agent_kernel/completion.py",
        "execution/verify.py",
    ):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "precommit" not in source, rel
        assert "check_source_for_edit" not in source, rel
    # The success mint is still exactly the verifier's three booleans.
    core = (ROOT / "harness" / "core.py").read_text(encoding="utf-8")
    assert "target_test_passed" in core
    assert "regression_passed" in core
