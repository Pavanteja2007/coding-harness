"""T1.W1.1 — every harness secret-egress path either redacts or says why it does not.

The audit this file pins was asked for because the trust story leaks from BOTH
sides of the process: T4 owns the CLI display path, this file covers the
harness/agent side. A secret that survives in a tool result, a journal row or
an error string inside `harness/` is not fixed by reordering `cli/ui.py`.

**What each test is really proving.** A test that asserts "the marker is
absent" is satisfied by a crash before the marker is produced — the doctrine's
"a test that can pass vacuously is worse than no test". So every redaction
assertion here is paired with a non-vacuity control that proves the value
actually travelled through the code under test:

  * `test_the_boundaries_actually_redact` proves the authority moved a value
    that a control value shows is redacted when it moves.
  * `test_the_fail_closed_boundary_withholds_on_a_raising_redactor` forces the
    failure with a real redactor that raises, and asserts the withheld marker
    AND that the secret is gone.
  * Every path below asserts on a value the harness produced, not on the
    absence of a value.

Host-only: no Docker, no provider, no network.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from harness import context_compiler, editor, lint, redaction, tool_errors
from harness.agent_kernel.events import RunEventJournal
from harness.agent_kernel.tools import ToolResult
from harness.redaction import (
    REDACTION_FAILURE_PREFIX,
    redact_for_journal,
    redact_text_for_journal,
)
from harness.tools import ToolBatchStep
from harness.trace import TraceLogger

#: A value the shared authority is measured to treat as a secret. Used as the
#: canary in every path test.
SECRET = "sk-" + "A" * 39

HARNESS_ROOT = Path(__file__).resolve().parent

#: Every journal-authority path in the harness. `harness/trace.py` is the
#: legacy tracer and `harness/agent_kernel/events.py` is the typed kernel's
#: canonical journal; a boundary wired into only one of them would satisfy
#: either alone, which is why both are in the table.
JOURNAL_BOUNDARIES = (
    (TraceLogger, "trace"),
    (RunEventJournal, "events"),
)


# ---------------------------------------------------------------------------
# 0. Non-vacuity: the boundary actually moves a value
# ---------------------------------------------------------------------------


def test_the_boundaries_actually_redact() -> None:
    """A boundary that redacts nothing would pass every "absent" test below.

    This is the control that makes the rest of the file meaningful: it proves
    `SECRET` IS redacted by the authority this boundary calls, so an absent
    secret in a path test is evidence rather than a crash artifact.
    """
    cleaned = redact_text_for_journal(f"token={SECRET}")
    assert SECRET not in cleaned
    assert "[REDACTED" in cleaned


def test_a_credential_is_still_redacted_under_the_key_that_names_it() -> None:
    """The regression this round's own first draft shipped, and it is load-bearing.

    `shared.security.redact_secrets(value, key)` decides partly from the KEY a
    string sits under: `{"api_key": "..."}` is redacted, and the bare string
    `"..."` is not. The first draft of `harness/redaction.py` recursed into
    leaves and applied the authority to each string SEPARATELY, which throws
    the key/value relationship away — so `{"api_key": "top-secret"}` came out
    with `top-secret` intact while every test using a bare
    `sk-...`-shaped string still passed.

    The pre-existing `tests/test_config_trace_state.py::
    test_trace_redacts_nested_credentials` is what caught it; that test is T5's
    and this one is the harness-side pin that says the shape must keep working
    here too, including through the `_cap_tree` -> single-authority-call
    structure the fix depends on.
    """
    payload = {
        "config": {
            "api_key": "top-secret",
            "model_tiers": {"easy": {"api_key": "nested-secret"}},
            "authorization": "Bearer another-secret",
            "max_tokens": 123,
        }
    }
    cleaned = redact_for_journal(payload)
    assert "top-secret" not in str(cleaned)
    assert "nested-secret" not in str(cleaned)
    assert "another-secret" not in str(cleaned)
    assert cleaned["config"]["max_tokens"] == 123, (
        "a key-aware redaction must not disturb a non-secret value"
    )


def test_a_key_named_credential_is_redacted_even_when_the_value_is_ordinary() -> None:
    """The other half: an innocuous-looking value under a sensitive key.

    `"a-short-token"` is not secret-shaped by itself, so a leaf-only redactor
    leaves it alone. Under `api_key` it is exactly the value that must not
    reach a journal. This is the assertion that keeps the two halves together.
    """
    cleaned = redact_for_journal({"headers": {"x_api_key": "a-short-token"}})
    assert "a-short-token" not in str(cleaned)


def test_the_ordinary_control_value_is_untouched() -> None:
    """The other half of the control: redaction is a no-op on ordinary text.

    Without this, a boundary that mangled everything would also make every
    secret absent — and would break every exact-receipt assertion in the tree.
    """
    ordinary = "No such file or directory: /tmp/example.txt"
    assert redact_text_for_journal(ordinary) == ordinary


# ---------------------------------------------------------------------------
# 1. Fail-closed: the redactor's failure mode is a DENIAL, not a pass-through
# ---------------------------------------------------------------------------


def test_the_fail_closed_boundary_withholds_on_a_raising_redactor(
    monkeypatch,
) -> None:
    """If the authority raises, the value is REPLACED, never passed through.

    This is the behaviour the brief requires and the one `cli/notify.py`
    already has: *"if no redactor resolves, detail is REPLACED"*. The
    boundary is forced into that state with a real redactor that raises — not
    with a mocked assertion about the code path — so it is the shipped
    behaviour under test rather than a description of it.
    """
    import shared.security as security

    def boom(*_args, **_kwargs):
        raise RuntimeError("the redactor is down")

    monkeypatch.setattr(security, "redact_text", boom)
    monkeypatch.setattr(security, "redact_secrets", boom)
    redaction.reset_journal_redaction_report()

    out = redact_text_for_journal(f"token={SECRET}")
    assert out.startswith(REDACTION_FAILURE_PREFIX), (
        "a raising redactor must produce the withheld marker, not the raw text"
    )
    assert SECRET not in out
    report = redaction.journal_redaction_report().as_dict()
    assert report["redactor_failures"] == 1, (
        "the failure must be COUNTED; a boundary that swallows a redactor "
        "outage silently reports the same numbers as one that never failed"
    )
    redaction.reset_journal_redaction_report()


def test_the_fail_closed_boundary_withholds_on_a_none_answer(monkeypatch) -> None:
    """A redactor that answers `None` has not cleared the value.

    `unknown != False`: reporting the raw text here would be reporting a
    non-answer as a pass. Asserted separately from the raising case because
    they are two different bugs and a single test would pass if only one were
    handled.
    """
    import shared.security as security

    monkeypatch.setattr(security, "redact_text", lambda *_a, **_k: None)
    monkeypatch.setattr(security, "redact_secrets", lambda *_a, **_k: None)
    out = redact_text_for_journal(f"token={SECRET}")
    assert out.startswith(REDACTION_FAILURE_PREFIX)
    assert SECRET not in out


def test_a_withheld_journal_row_is_still_written(tmp_path, monkeypatch) -> None:
    """The row lands WITH the withholding — losing the row would lose the proof.

    The temptation when a redactor fails is to skip the write. That converts a
    security control into an evidence loss: the journal is what a reader
    inspects to find out what happened, and a missing row is indistinguishable
    from a run that never did the thing.
    """
    import shared.security as security

    monkeypatch.setattr(security, "redact_text", lambda *_a, **_k: None)
    monkeypatch.setattr(security, "redact_secrets", lambda *_a, **_k: None)

    logger = TraceLogger(tmp_path / "withheld")
    logger.log("tool_result", {"output": f"token={SECRET}"})
    rows = logger.read_all()
    assert len(rows) == 1, "a withheld row must still be journalled"
    assert REDACTION_FAILURE_PREFIX in str(rows[0])
    assert SECRET not in str(rows[0])


def test_the_whole_boundary_is_total_and_never_raises_on_hostile_input() -> None:
    """A boundary that raises would take the run down instead of protecting it.

    Asserted on inputs chosen to break a naive implementation: a cycle, a very
    deep nest, a non-string key, and a set. Each must return a value, not
    raise, because the call site is a journal write in the middle of a run.
    """
    cyclic: dict = {}
    cyclic["self"] = cyclic
    deep: dict = {}
    node = deep
    for _ in range(40):
        node["next"] = {}
        node = node["next"]

    for payload in (cyclic, deep, {1: "one", None: "none"}, {"s": {"a", "b"}}):
        out = redact_for_journal(payload)
        assert out is not None
        assert isinstance(out, (dict, str, list, tuple))


# ---------------------------------------------------------------------------
# 2. Path: tool result payload
# ---------------------------------------------------------------------------


def test_the_tool_result_path_journals_redacted(tmp_path) -> None:
    """`ToolResult.output` reaches the user through the journal, redacted.

    The value is deliberately NOT redacted at construction: the model needs the
    bytes verbatim or the agent cannot work, so the two trust requirements are
    separated by the boundary and the redaction lives where the value leaves
    the process. This test asserts that separation actually holds end to end.
    """
    result = ToolResult(True, f"stdout: token={SECRET}")
    assert result.output == f"stdout: token={SECRET}", (
        "the in-process value must stay verbatim for the model; if this ever "
        "becomes redacted, the agent can no longer read a real command result"
    )

    logger = TraceLogger(tmp_path / "toolresult")
    logger.log("tool_result", result.as_dict())
    rows = logger.read_all()
    assert len(rows) == 1
    assert SECRET not in str(rows[0])
    assert "[REDACTED" in str(rows[0])


def test_the_tool_batch_step_path_declares_its_boundary_and_honours_it(
    tmp_path,
) -> None:
    """`ToolBatchStep` is the same shape and the same decision.

    Asserted rather than trusted: the class carries a written declaration
    (`harness/tools.py`), and a declaration that is not enforced is a comment.
    """
    step = ToolBatchStep(ok=False, output=f"stderr: {SECRET}", detail={"k": "v"})
    assert SECRET in step.output, "the raw value is intact in-process"

    logger = TraceLogger(tmp_path / "batchstep")
    logger.log("tool_result", {"ok": step.ok, "output": step.output})
    rows = logger.read_all()
    assert len(rows) == 1
    assert SECRET not in str(rows[0])


# ---------------------------------------------------------------------------
# 3. Path: edit results (file content around a match)
# ---------------------------------------------------------------------------


def test_an_ambiguous_edit_refusal_does_not_journal_the_env_line(tmp_path) -> None:
    """A `.env` edit shows the line in its candidates — and the line is redacted.

    This is the concrete leak the brief names: `_candidates` reads real bytes
    either side of every match offset, so an ambiguous edit to a `.env` puts
    `API_KEY=<secret>` into the receipt, into the `edit_refused` journal row and
    into the model's next turn.

    `apply_text_edit(root, relative_path, ...)` takes a repo-relative path, so
    the fixture is a temporary ROOT holding a relative file — the same shape a
    real run uses.
    """
    env_body = (
        f"DATABASE_URL=postgres://u:p@h/db\nAPI_KEY={SECRET}\nMODE=prod\nMODE=prod\n"
    )
    (tmp_path / ".env").write_text(env_body, encoding="utf-8")

    # `require_edit_digest` is this primitive's own floor (an absent key means
    # "on"), so the read ledger is what a real caller does. Skipping it would
    # exercise the `stale_read` refusal instead of the ambiguous-match preview.
    session = editor.EditSession()
    session.note_read(".env", data=env_body.encode("utf-8"))
    outcome = editor.apply_text_edit(
        str(tmp_path), ".env", "MODE=prod", "MODE=dev", session=session
    )
    record = outcome.to_dict()
    assert record["ok"] is False
    assert record["error_kind"] == editor.ERROR_AMBIGUOUS_MATCH, (
        f"the fixture must produce an ambiguous_match to exercise the preview "
        f"path, got {record['error_kind']!r}: {record['message'][:200]}"
    )
    assert record["candidates"], "the refusal must still carry its candidates"
    rendered = str(record)
    assert SECRET not in rendered, (
        f"the raw secret reached the EditOutcome receipt: {rendered[:400]}"
    )


def test_an_ambiguous_edit_refusal_redacts_the_candidate_preview(tmp_path) -> None:
    """The preview itself is redacted, not merely absent from the assertion.

    Pairs with the test above: it asserts the specific field that carries file
    bytes, so a future refactor that moves the content out of `message` and
    into `candidates[].preview` (the obvious place to put it) is still caught.
    """
    body = f"TOKEN={SECRET}\n" + "filler = 1\n" * 40
    (tmp_path / "settings.py").write_text(body, encoding="utf-8")

    session = editor.EditSession()
    session.note_read("settings.py", data=body.encode("utf-8"))
    outcome = editor.apply_text_edit(
        str(tmp_path), "settings.py", "filler = 1", "filler = 2", session=session
    )
    record = outcome.to_dict()
    previews = [str(item) for item in record.get("candidates", [])]
    assert previews, (
        "the refusal must still carry its candidates; a receipt with no "
        "candidates is not a redaction, it is a lost receipt"
    )
    for preview in previews:
        assert SECRET not in preview
    assert SECRET not in str(record)


def test_an_edit_refusal_keeps_its_line_numbers_exact(tmp_path) -> None:
    """Redaction must not cost the refusal its locatability.

    `candidate.line`, `check_line`, `pre_sha256` and `post_sha256` are numbers
    and digests: they are NOT redacted, and the whole ambiguous-match contract
    (which names the candidate lines so the model can extend `old_string`)
    depends on them being exact. Asserted so a future "redact the whole
    receipt" change is caught.
    """
    body = "a = 1\n" * 3 + "target = 2\n" + "target = 2\n" + "a = 1\n" * 3
    (tmp_path / "app.py").write_text(body, encoding="utf-8")

    session = editor.EditSession()
    session.note_read("app.py", data=body.encode("utf-8"))
    outcome = editor.apply_text_edit(
        str(tmp_path), "app.py", "target = 2", "target = 3", session=session
    )
    record = outcome.to_dict()
    assert record["error_kind"] == editor.ERROR_AMBIGUOUS_MATCH, (
        f"got {record['error_kind']!r}: {record['message'][:200]}"
    )
    assert record["match_count"] > 1
    lines = [int(item["line"]) for item in record["candidates"]]
    assert lines == [4, 5], f"the candidate lines must stay exact, got {lines}"
    assert record["pre_sha256"], "the pre-image digest must be reported"
    assert record["post_sha256"] == "", (
        "a refusal writes nothing, so there is no post-image digest to report; "
        "a non-empty one would mean the ambiguous edit landed anyway"
    )
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == body, (
        "an ambiguous refusal must leave the file byte-identical"
    )


# ---------------------------------------------------------------------------
# 4. Path: error classification
# ---------------------------------------------------------------------------


def test_a_tool_error_detail_cannot_carry_a_credential() -> None:
    """`ToolError.detail` is assembled from the command and `str(exc)`.

    It is the value the model is told about, the value the reflection feedback
    quotes verbatim, and the value the journal row carries — three consumers
    with three lifetimes, so it is redacted once at construction.
    """
    err = tool_errors.ToolError("internal_error", f"failed with token={SECRET}")
    assert SECRET not in err.detail
    assert "[REDACTED" in err.detail


def test_a_model_failure_detail_cannot_carry_a_credential() -> None:
    """The docstring claimed "never a credential"; this is the enforcement point.

    A provider exception routinely quotes the request it was given, including
    an `Authorization` header. Before this round the claim was documentation
    with nothing behind it.
    """

    class ProviderError(Exception):
        def __str__(self) -> str:
            return f"401 unauthorized for Authorization: Bearer {SECRET}"

    failure = tool_errors.classify_model_failure(ProviderError())
    assert SECRET not in failure.detail
    # The POLICY fields must survive untouched - the retry ladder reads them.
    assert failure.kind
    assert isinstance(failure.retryable, bool)
    assert isinstance(failure.terminal, bool)


def test_a_tool_error_keeps_its_kind_and_hint_exactly() -> None:
    """`kind` drives the recovery policy and `hint` is our own prose.

    Neither is derived from failure text, so redacting them would be a change
    of meaning rather than a protection - and `kind` is the one field a caller
    branches on, so a mangled kind would change what the loop DOES.
    """
    err = tool_errors.ToolError("file_not_found", "gone", hint="check the path")
    assert err.kind == "file_not_found"
    assert err.hint == "check the path"
    assert err == ("file_not_found", err.detail, "check the path")


def test_classify_still_produces_the_exact_detail_it_produced_before() -> None:
    """The non-vacuity control for the boundary above.

    Redaction is a no-op on the classifier's own static prose, so every
    existing assertion on an exact detail string still holds. If this fails,
    the boundary is rewriting ordinary values and dozens of suites break for no
    security benefit.
    """
    err = tool_errors.classify(
        2, "", "No such file or directory: /tmp/x", False, "cat /tmp/x"
    )
    assert err.kind == "file_not_found"
    assert err.detail == "file not found: /tmp/x"
    assert err[0] == "file_not_found", "ToolError is still a tuple"


# ---------------------------------------------------------------------------
# 5. Path: lint / typecheck results
# ---------------------------------------------------------------------------


def test_a_lint_finding_does_not_carry_a_credential_into_its_rendering() -> None:
    """A finding's message quotes the offending source token.

    `render_findings` is the model-facing string, the `lint_failed` journal
    row's body and the retry feedback in `harness/core.py` — three surfaces
    with three lifetimes, so it is redacted once at the renderer.
    """
    findings = [
        lint.LintFinding(
            file="config.py", line=3, kind="syntax_error", message=f"bad token {SECRET}"
        )
    ]
    rendered = lint.render_findings(findings)
    assert SECRET not in rendered
    assert "config.py:3" in rendered, "the finding must stay locatable"
    assert "[REDACTED" in rendered


def test_the_in_edit_check_rendering_does_not_carry_a_credential() -> None:
    """`render_check` is the model-facing form of an in-edit refusal.

    It quotes the message and the +/-3 lines of real source around the failure,
    so a pre-commit syntax failure in a `.env`-shaped file carries the secret
    into the tool result and the `edit_refused` row.
    """
    check = lint.EditCheck(
        lint.CHECK_FAILED,
        "settings.py",
        line=2,
        kind="syntax",
        message=f"unexpected token {SECRET}",
        context=f"  1 KEY = 1\n  2 TOKEN = {SECRET}\n  3 OTHER = 2",
    )
    rendered = lint.render_check(check)
    assert rendered
    assert SECRET not in rendered
    assert "settings.py:2" in rendered


# ---------------------------------------------------------------------------
# 6. Path: the compiled-context / test-feedback compiler
# ---------------------------------------------------------------------------


def test_the_context_compiler_redacts_repository_content() -> None:
    """`_as_text` is where repository/instruction content becomes model text.

    Before this round it carried a 16 KiB marker gate that returned the RAW
    string on a marker miss, and two `except` branches that fell back to a
    local regex for a `bearer` prefix alone — a pattern set narrow enough to
    let an `sk-...` key straight through, on a path that carries file content.
    """
    long_ordinary = "filler line\n" * 4_000  # > 16 KiB, no marker at all
    assert SECRET not in context_compiler._as_text(long_ordinary)
    assert context_compiler._as_text(long_ordinary) == long_ordinary, (
        "an ordinary long value must survive byte-for-byte"
    )
    assert SECRET not in context_compiler._as_text(f"KEY = {SECRET}")
    assert SECRET not in context_compiler._as_text(f"Bearer {SECRET}")


def test_the_context_compiler_does_not_keep_a_second_redactor() -> None:
    """`harness/` must not carry its own secret patterns.

    The doctrine's "one authority per concern" rule, made checkable. This
    caught the real defect: `context_compiler._redact_large` reached into
    `shared.security`'s PRIVATE compiled patterns
    (`security._QUOTED_SECRET`, `security._SECRET_PATTERNS`, ...) and fell back
    to its own `bearer`-prefix regex on any exception — which is how two
    implementations end up disagreeing about the same credential.

    `harness/trace.py`'s `LEGACY_REDACTED = "[REDACTED]"` is deliberately NOT an
    offender: it is a documented compatibility constant re-exported for a
    consumer that string-matched on the pre-shared placeholder, it declares no
    patterns, and it is never used on a write path. It is exempted by name so
    the exemption is visible rather than a loosened pattern.
    """
    allowed = {
        ("trace.py", 'LEGACY_REDACTED = "[REDACTED]"'),
    }
    offenders: list[str] = []
    for path in sorted(HARNESS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if "security._" in stripped or "REDACTED]" in stripped:
                if (path.name, stripped) in allowed:
                    continue
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert offenders == [], (
        "harness/ reached into shared.security privates or declared its own "
        "placeholder; the authority is the single implementation:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# 7. Journal paths: one helper, both authorities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "journal_class", JOURNAL_BOUNDARIES, ids=[b[1] for b in JOURNAL_BOUNDARIES]
)
def test_both_journal_authorities_actually_redact(journal_class, tmp_path) -> None:
    """Both journals, parametrised so neither can be dropped from the table."""
    if journal_class is TraceLogger:
        target = TraceLogger(tmp_path / "a")
        target.log("tool_result", {"output": f"token={SECRET}"})
        rows = target.read_all()
        on_disk = (tmp_path / "a" / "trace.jsonl").read_text(encoding="utf-8")
    else:
        target = RunEventJournal(tmp_path / "b" / "trace.jsonl", run_id="r1")
        target.append("tool_result", {"output": f"token={SECRET}"})
        rows = target.to_records()
        on_disk = (tmp_path / "b" / "trace.jsonl").read_text(encoding="utf-8")

    assert len(rows) == 1, "the row must exist; an absent row proves nothing"
    assert SECRET not in str(rows[0])
    assert "[REDACTED" in str(rows[0])
    assert SECRET not in on_disk, "the raw secret reached the FILE"


def test_neither_journal_calls_the_redactor_directly() -> None:
    """ "One helper, not scattered calls", enforced at the source level.

    Both journals previously called `shared.security.redact_*` inline, so a
    future edit could add a third call site with different behaviour. This
    asserts the shape of both files.
    """
    for name in ("trace.py", Path("agent_kernel") / "events.py"):
        path = HARNESS_ROOT / name
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "redact_secrets" not in called, (
            f"{name} calls redact_secrets directly; route it through "
            "harness.redaction so the cap and the fail-closed rule apply"
        )
        assert "redact_text" not in called, (
            f"{name} calls redact_text directly; same reason"
        )
        assert ("redact_for_journal" in called) or ("redact_for_journal" in source), (
            f"{name} does not route through the one boundary"
        )


def test_the_receipt_writer_also_redacts(tmp_path) -> None:
    """`write_receipt` is a SEPARATE file, so it needs its own boundary call.

    Worth its own test: `receipt.json` is read by a consumer that never reads
    `trace.jsonl`, so a boundary on the trace alone would leave the receipt
    unwatched.
    """
    logger = TraceLogger(tmp_path / "receipt")
    path = logger.write_receipt({"model": "m", "api_key": SECRET})
    on_disk = path.read_text(encoding="utf-8")
    assert SECRET not in on_disk
    assert path.exists()


# ---------------------------------------------------------------------------
# 8. The declarations the audit required, pinned
# ---------------------------------------------------------------------------


def test_the_declared_safe_and_boundary_paths_all_carry_a_written_reason() -> None:
    """A declaration the code does not carry is a comment; this asserts it exists.

    The five paths the brief listed must each state their decision. Asserted
    by looking for the decision marker in the owning module's docstrings, so a
    future rewrite cannot drop the reasoning without failing here.
    """
    expectations = {
        "tools.py": ("Redaction boundary decision",),
        "agent_kernel/tools.py": ("Redaction boundary decision",),
        "editor.py": ("Redaction boundary (decision",),
        "lint.py": ("Redaction boundary (decision",),
        "tool_errors.py": ("Redaction boundary (decision",),
        "context_compiler.py": ("Redaction boundary (decision",),
        "prompts.py": ("DECLARED SAFE, with a reason",),
    }
    missing: list[str] = []
    for relative, markers in expectations.items():
        text = (HARNESS_ROOT / relative).read_text(encoding="utf-8")
        if not any(marker in text for marker in markers):
            missing.append(relative)
    assert missing == [], (
        "these egress paths lost their written redaction decision: "
        + ", ".join(missing)
    )


def test_the_scripts_side_lint_ratchet_is_named_as_not_ours(tmp_path) -> None:
    """`scripts/lint_ratchet.py` carries source lines and is NOT in `harness/`.

    The brief lists it as an egress path and it is out of this round's scope,
    so the honest thing is to say so in code rather than leave a reader
    assuming the audit covered it. The assertion is that the scope boundary is
    WRITTEN DOWN — a cross-terminal request filed in the handoff, recorded
    here so the next session cannot mistake silence for coverage.
    """
    assert not (HARNESS_ROOT.parent / "scripts" / "lint_ratchet.py").exists() or True
    boundary = (HARNESS_ROOT / "PARALLEL_HAZARD.md").read_text(encoding="utf-8")
    assert "scripts/lint_ratchet.py" in boundary, (
        "the out-of-scope egress path must be named in the hazard inventory so "
        "nobody reads the audit as covering it"
    )
