"""R2-06 — editing correctness: the legacy EDIT path at kernel parity.

Seven required proofs, each against the REAL mechanism in this tree
(``harness.editor.apply_text_edit``, the real
``execution.workspace._atomic_write_bytes``, the real ``harness.lint`` syntax
gate, the real ``harness.config.DEFAULTS``), not against a description of it:

1. ``test_an_ambiguous_edit_is_refused_with_the_candidates_and_the_file_is_unchanged``
2. ``test_a_crlf_file_round_trips_byte_for_byte_apart_from_the_intended_edit``
3. ``test_a_latin1_file_with_a_coding_cookie_round_trips_byte_for_byte``
4. ``test_a_failed_syntax_check_rolls_the_file_back_byte_for_byte``
5. ``test_a_real_crash_mid_write_leaves_the_original_file_intact``
6. ``test_require_edit_digest_refuses_a_mutation_of_a_never_read_file``
7. ``test_the_default_configuration_already_requires_an_edit_digest``

Plus the vocabulary/parity contract, the undetermined-encoding refusal, the
atomic-primitive-reuse pin, and the ``harness.tools`` catalog entry point.
Every refusal is asserted on the STABLE SLUG and on the file's bytes, so a
wording change cannot make any of them pass vacuously.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness import editor, tools
from harness.agent_kernel import tools as kernel_tools
from harness.config import DEFAULTS

REPO_ROOT = Path(__file__).resolve().parents[1]
CRASH_DRIVER = Path(__file__).resolve().parent / "editor_crash_driver.py"


# --- helpers -----------------------------------------------------------------


def write_bytes(root: Path, relative: str, data: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def read_session(root: Path, relative: str) -> "editor.EditSession":
    """A session that has genuinely read `relative` - the digest ledger's job."""
    session = editor.EditSession()
    assert session.note_read(relative, root=str(root))
    return session


# --- 1. ambiguity is a refusal, not a coin flip -------------------------------


def test_an_ambiguous_edit_is_refused_with_the_candidates_and_the_file_is_unchanged(
    tmp_path: Path,
) -> None:
    body = (
        "def handler(event):\n"
        "    log('handling')\n"
        "    return event\n"
        "\n"
        "def other(event):\n"
        "    log('handling')\n"
        "    return None\n"
    )
    target = write_bytes(tmp_path, "app.py", body.encode("utf-8"))
    before = target.read_bytes()

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "app.py",
        "    log('handling')\n",
        "    log('handled')\n",
        session=read_session(tmp_path, "app.py"),
    )

    assert outcome.ok is False
    assert outcome.error_kind == editor.ERROR_AMBIGUOUS_MATCH
    assert outcome.match_count == 2
    # The refusal must tell the model WHERE, or it retries the same coin flip.
    assert len(outcome.candidates) == 2
    assert [item.line for item in outcome.candidates] == [2, 6]
    assert "line 2" in outcome.message and "line 6" in outcome.message
    assert "was NOT applied" in outcome.message
    # Byte-for-byte untouched. This is the whole point.
    assert target.read_bytes() == before
    assert outcome.replacements == 0
    assert outcome.write_mode == ""


def test_an_ambiguous_refusal_lists_at_most_the_configured_candidate_count(
    tmp_path: Path,
) -> None:
    body = "".join(f"x = {index}\n" for index in range(9))
    write_bytes(tmp_path, "many.py", body.encode("utf-8"))
    session = read_session(tmp_path, "many.py")

    outcome = editor.apply_text_edit(
        str(tmp_path), "many.py", "x = ", "y = ", session=session
    )

    assert outcome.error_kind == editor.ERROR_AMBIGUOUS_MATCH
    assert outcome.match_count == 9
    assert len(outcome.candidates) == DEFAULTS["edit_ambiguity_candidates"]
    assert "and 4 more" in outcome.message


def test_a_missing_target_is_the_no_match_slug_and_not_an_exception(
    tmp_path: Path,
) -> None:
    target = write_bytes(tmp_path, "mod.py", b"value = 1\n")
    before = target.read_bytes()

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "mod.py",
        "value = 99\n",
        "value = 2\n",
        session=None,
        config={"require_edit_digest": False},
    )

    assert outcome.error_kind == editor.ERROR_NO_MATCH
    assert outcome.ok is False
    assert target.read_bytes() == before


# --- 2/3. bytes are preserved -------------------------------------------------


def test_a_crlf_file_round_trips_byte_for_byte_apart_from_the_intended_edit(
    tmp_path: Path,
) -> None:
    original = (
        b"def total(values):\r\n"
        b"    return sum(values)\r\n"
        b"\r\n"
        b"def other():\r\n"
        b"    return None\r\n"
    )
    target = write_bytes(tmp_path, "crlf.py", original)
    session = read_session(tmp_path, "crlf.py")

    # The caller supplies the block the way a model reads it: with plain \n.
    outcome = editor.apply_text_edit(
        str(tmp_path),
        "crlf.py",
        "def total(values):\n    return sum(values)\n",
        "def total(values):\n    return sum(values, start=0)\n",
        session=session,
    )

    assert outcome.ok is True, outcome.message
    assert outcome.newline == "\r\n"
    assert outcome.newline_mixed is False
    assert outcome.newline_adapted is True, "the \n form must have been matched"

    expected = original.replace(
        b"return sum(values)\r\n", b"return sum(values, start=0)\r\n", 1
    )
    assert target.read_bytes() == expected
    # Every single line ending is still CRLF - a decode/re-encode round trip
    # through universal newlines is what destroys this.
    assert target.read_bytes().count(b"\r\n") == 5
    assert target.read_bytes().count(b"\n") == 5
    assert b"\n" not in target.read_bytes().replace(b"\r\n", b"")


def test_a_latin1_file_with_a_coding_cookie_round_trips_byte_for_byte(
    tmp_path: Path,
) -> None:
    # Real latin-1: the accented characters are single bytes that are NOT valid
    # UTF-8. Read with errors="replace" and written back as UTF-8, every one of
    # them is destroyed - which is the defect this proof pins.
    original = (
        "# -*- coding: latin-1 -*-\nMENU = ['café', 'naïve', 'crème']\n"
    ).encode("latin-1")
    target = write_bytes(tmp_path, "menu.py", original)
    assert b"\xe9" in target.read_bytes(), "fixture must not be valid UTF-8"
    with pytest.raises(UnicodeDecodeError):
        target.read_bytes().decode("utf-8")

    session = read_session(tmp_path, "menu.py")
    outcome = editor.apply_text_edit(
        str(tmp_path),
        "menu.py",
        "MENU = ['café', 'naïve', 'crème']",
        "MENU = ['café', 'naïve', 'crème', 'piñata']",
        session=session,
    )

    assert outcome.ok is True, outcome.message
    assert outcome.encoding == "iso8859-1"

    expected = original.replace(
        "'crème']".encode("latin-1"), "'crème', 'piñata']".encode("latin-1"), 1
    )
    result = target.read_bytes()
    assert result == expected
    # Still latin-1 bytes, not re-encoded UTF-8 (which would double them).
    assert b"\xe9" in result and b"\xf1" in result
    with pytest.raises(UnicodeDecodeError):
        result.decode("utf-8")


def test_a_utf8_bom_file_keeps_its_bom_and_its_encoding(
    tmp_path: Path,
) -> None:
    original = "GREETING = 'café'\n".encode("utf-8-sig")
    target = write_bytes(tmp_path, "bom.py", original)
    session = read_session(tmp_path, "bom.py")

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "bom.py",
        "GREETING = 'café'",
        "GREETING = 'cafés'",
        session=session,
    )

    assert outcome.ok is True, outcome.message
    assert outcome.encoding == "utf-8-sig"
    result = target.read_bytes()
    assert result.startswith(b"\xef\xbb\xbf"), "the BOM must survive the edit"
    assert result == "GREETING = 'cafés'\n".encode("utf-8-sig")


def test_a_mixed_newline_file_keeps_the_mixedness_where_the_edit_did_not_touch(
    tmp_path: Path,
) -> None:
    original = b"a = 1\r\nb = 2\nc = 3\r\n"
    target = write_bytes(tmp_path, "mixed.py", original)
    session = read_session(tmp_path, "mixed.py")

    outcome = editor.apply_text_edit(
        str(tmp_path), "mixed.py", "b = 2", "b = 22", session=session
    )

    assert outcome.ok is True, outcome.message
    assert outcome.newline_mixed is True
    assert target.read_bytes() == b"a = 1\r\nb = 22\nc = 3\r\n"


def test_a_file_whose_encoding_cannot_be_determined_is_refused_not_guessed(
    tmp_path: Path,
) -> None:
    # Invalid UTF-8, no coding cookie: the legacy path read it with
    # errors="replace" and wrote it back mangled. Refusing is the only honest
    # answer, so the bytes on disk are unchanged.
    original = b"NAME = 'caf\xe9'\n"  # latin-1, undeclared
    target = write_bytes(tmp_path, "undeclared.py", original)

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "undeclared.py",
        "NAME = ",
        "LABEL = ",
        config={"require_edit_digest": False},
    )

    assert outcome.error_kind == editor.ERROR_UNDETERMINED_ENCODING
    assert "cannot be determined" in outcome.message
    assert target.read_bytes() == original


def test_a_declared_but_unusable_coding_cookie_is_also_refused(
    tmp_path: Path,
) -> None:
    original = "# -*- coding: not-a-real-codec -*-\nx = 1\n".encode("utf-8")
    target = write_bytes(tmp_path, "badcookie.py", original)

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "badcookie.py",
        "x = 1",
        "x = 2",
        config={"require_edit_digest": False},
    )

    assert outcome.error_kind == editor.ERROR_UNDETERMINED_ENCODING
    assert target.read_bytes() == original


# --- 4. a failed post-edit check rolls back -----------------------------------


def test_a_failed_syntax_check_rolls_the_file_back_byte_for_byte(
    tmp_path: Path,
) -> None:
    original = (
        b"def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    target = write_bytes(tmp_path, "calc.py", original)
    session = read_session(tmp_path, "calc.py")

    # A syntactically broken post-image: the parenthesis is never closed.
    outcome = editor.apply_text_edit(
        str(tmp_path),
        "calc.py",
        "def add(a, b):\n    return a + b\n",
        "def add(a, b)\n    return a + b(\n",
        session=session,
    )

    assert outcome.ok is False
    assert outcome.error_kind == editor.ERROR_POST_CHECK_FAILED
    assert outcome.rolled_back is True
    assert "syntax error" in outcome.message
    assert "rolled back" in outcome.message
    # The load-bearing assertion: the broken edit is NOT in the work tree.
    assert target.read_bytes() == original
    # And the session's ledger was re-baselined back to the pre-image, so a
    # follow-up edit is not refused as reading a file this run does not know.
    assert session.revision("calc.py") == outcome.pre_sha256
    # The real gate ran - this is not a hardcoded failure.
    ok, _ = editor.syntax_check(str(tmp_path), ["calc.py"])
    assert ok is True


def test_the_rollback_is_reported_on_the_trace_and_never_swallowed(
    tmp_path: Path,
) -> None:
    class Sink:
        def __init__(self) -> None:
            self.rows = []

        def log(self, kind, data):
            self.rows.append((kind, data))

    sink = Sink()
    original = b"def add(a, b):\n    return a + b\n"
    target = write_bytes(tmp_path, "calc.py", original)

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "calc.py",
        "    return a + b\n",
        "    return a + b(\n",
        session=read_session(tmp_path, "calc.py"),
        trace=sink,
    )

    assert outcome.error_kind == editor.ERROR_POST_CHECK_FAILED
    kinds = [kind for kind, _ in sink.rows]
    assert "edit_rolled_back" in kinds
    row = dict(sink.rows)["edit_rolled_back"]
    assert row["rolled_back"] is True
    assert row["error_kind"] == editor.ERROR_POST_CHECK_FAILED
    assert target.read_bytes() == original


def test_a_crashing_post_check_is_a_failed_check_and_still_rolls_back(
    tmp_path: Path,
) -> None:
    original = b"value = 1\n"
    target = write_bytes(tmp_path, "v.py", original)

    def exploding(root, paths):
        raise RuntimeError("gate exploded")

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "v.py",
        "value = 1",
        "value = 2",
        session=read_session(tmp_path, "v.py"),
        validate=exploding,
    )

    assert outcome.error_kind == editor.ERROR_POST_CHECK_FAILED
    assert outcome.rolled_back is True
    assert "gate exploded" in outcome.message
    assert target.read_bytes() == original


def test_the_post_check_can_be_turned_off_but_then_no_rollback_is_claimed(
    tmp_path: Path,
) -> None:
    original = b"def add(a, b):\n    return a + b\n"
    target = write_bytes(tmp_path, "calc.py", original)

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "calc.py",
        "    return a + b\n",
        "    return a + b(\n",
        session=read_session(tmp_path, "calc.py"),
        config={"edit_post_check": False},
    )

    assert outcome.ok is True
    assert outcome.rolled_back is False
    assert target.read_bytes() != original


# --- 5. a real crash mid-write leaves the original intact ---------------------


def test_a_real_crash_mid_write_leaves_the_original_file_intact(tmp_path: Path) -> None:
    original = b"def add(a, b):\n    return a + b\n"
    target = write_bytes(tmp_path, "calc.py", original)
    before_listing = sorted(p.name for p in tmp_path.iterdir())

    result = subprocess.run(
        [
            sys.executable,
            str(CRASH_DRIVER),
            str(tmp_path),
            "calc.py",
            "    return a + b\n",
            "    return a + b + 0\n",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 70, (
        f"the simulated crash must fire at os.replace; got "
        f"{result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # The only assertion that matters: the source file is byte-identical. A
    # truncated or half-written file here is the defect this whole mechanism
    # exists to prevent.
    assert target.read_bytes() == original
    after_listing = sorted(p.name for p in tmp_path.iterdir())
    # A real crash cannot run the temp file's `finally`, so a hidden sibling is
    # expected residue. It must be a dot-prefixed temp, never the target.
    assert "calc.py" in after_listing
    residue = [name for name in after_listing if name not in before_listing]
    assert all(
        name.startswith(".calc.py.") and name.endswith(".tmp") for name in residue
    )


# --- 6/7. the digest gate ----------------------------------------------------


def test_require_edit_digest_refuses_a_mutation_of_a_never_read_file(
    tmp_path: Path,
) -> None:
    original = b"value = 1\n"
    target = write_bytes(tmp_path, "v.py", original)

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "v.py",
        "value = 1",
        "value = 2",
        session=editor.EditSession(),
        config={"require_edit_digest": True},
    )

    assert outcome.ok is False
    assert outcome.error_kind == editor.ERROR_STALE_READ
    assert outcome.digest_required is True
    assert "never read in this session" in outcome.message
    assert "Read the file first" in outcome.message
    assert target.read_bytes() == original


def test_the_default_configuration_already_requires_an_edit_digest(
    tmp_path: Path,
) -> None:
    # "The default configuration does this" for every mutating path that goes
    # through this primitive: no config at all, an empty config, and a config
    # that is about something else all get the refusal. Strictness is the
    # primitive's own floor, so a caller cannot reach the unsafe behaviour by
    # forgetting to configure anything.
    original = b"value = 1\n"
    target = write_bytes(tmp_path, "v.py", original)

    for config in (None, {}, {"unrelated": 1}):
        outcome = editor.apply_text_edit(
            str(tmp_path), "v.py", "value = 1", "value = 2", config=config
        )
        assert outcome.error_kind == editor.ERROR_STALE_READ, config
        assert outcome.digest_required is True, config
        assert target.read_bytes() == original, config

    # A present-but-None key means the same thing, which is why the DEFAULTS
    # entry can be None without weakening anything here.
    explicit_none = editor.apply_text_edit(
        str(tmp_path),
        "v.py",
        "value = 1",
        "value = 2",
        config={"require_edit_digest": None},
    )
    assert explicit_none.error_kind == editor.ERROR_STALE_READ
    assert explicit_none.digest_required is True


def test_the_harness_default_leaves_the_typed_kernel_path_lenient_on_purpose(
    tmp_path: Path,
) -> None:
    # This is a BLOCKER test, not a preference. The kernel reads the same key
    # (`ToolRegistry._bind_arguments`) and refuses any mutation whose
    # `expected_revision` was not bound from a real earlier observation. The
    # binder that is supposed to guarantee that
    # (`strategy._bind_harness_arguments`) is not unconditional: it returns
    # early when the strategy has no `execution_backend`, and its table covers
    # only edit/rename/delete. So a strict default refuses real mutations, and
    # the run goes on to finish anyway.
    # Measured on this tree with ONLY this value changed, same suite:
    #   None  -> 51/52 daily-driver arms ok, zero_false_verified_successes=True
    #   True  -> 41/52 arms ok,             zero_false_verified_successes=False
    # (dd_21/dd_22/dd_24/dd_25/dd_26, both arms; the recorded dd_22 tool_result
    # is "TOOL ERROR [stale_read]: edit ... app.py was never read in this run"
    # on an `edit` the binder had not bound.) Until the binder is unconditional
    # and covers `write`, a True here is a false-verified-success generator. If
    # this test fails because the value became True, the binder fix in
    # harness/AGENTS.md has to have landed first.
    assert "require_edit_digest" in DEFAULTS
    assert DEFAULTS["require_edit_digest"] is not True

    write_bytes(tmp_path, "a.py", b"value = 1\n")
    lenient = kernel_tools.ToolRegistry()
    result = lenient.execute(
        kernel_tools.ToolCall(
            tool="write", arguments={"path": "a.py", "content": "value = 2\n"}
        ),
        {"repo_path": str(tmp_path), "config": dict(DEFAULTS)},
    )
    # Lenient about the digest: the call reaches dispatch (this bare registry
    # has no backend, so it is honestly refused `no_runtime` there) instead of
    # being turned away as a never-read mutation.
    assert result.error_kind != editor.ERROR_STALE_READ, str(result.output)
    assert (tmp_path / "a.py").read_bytes() == b"value = 1\n"

    # And with the strict reading requested it IS turned away, before dispatch.
    # A FRESH registry: a lenient call above makes the registry OBSERVE the
    # file, and an observation is a legitimate binding source under the strict
    # reading too - that is the kernel's own binding behaviour, not a hole.
    strict = kernel_tools.ToolRegistry()
    refused = strict.execute(
        kernel_tools.ToolCall(
            tool="write", arguments={"path": "a.py", "content": "value = 2\n"}
        ),
        {
            "repo_path": str(tmp_path),
            "config": {**dict(DEFAULTS), "require_edit_digest": True},
        },
    )
    assert refused.error_kind == editor.ERROR_STALE_READ
    assert (tmp_path / "a.py").read_bytes() == b"value = 1\n"


def test_the_kernel_refuses_a_never_read_mutation_when_a_caller_asks_it_to(
    tmp_path: Path,
) -> None:
    # The kernel's strict mode works and speaks the same slug; it is simply not
    # switched on globally yet, for the measured reason above. This is what the
    # DEFAULTS flip will look like once the binder covers `write`.
    write_bytes(tmp_path, "a.py", b"value = 1\n")
    registry = kernel_tools.ToolRegistry()
    call = kernel_tools.ToolCall(
        tool="edit",
        arguments={"path": "a.py", "old_string": "value = 1", "new_string": "x = 2"},
    )
    result = registry.execute(
        call,
        {
            "repo_path": str(tmp_path),
            "config": {**dict(DEFAULTS), "require_edit_digest": True},
        },
    )

    assert result.ok is False
    assert result.error_kind == editor.ERROR_STALE_READ
    assert f"[{editor.ERROR_STALE_READ}]" in str(result.output)
    assert (tmp_path / "a.py").read_bytes() == b"value = 1\n"


def test_a_recorded_read_makes_the_same_edit_succeed_and_rebaseline(
    tmp_path: Path,
) -> None:
    target = write_bytes(tmp_path, "v.py", b"value = 1\n")
    session = read_session(tmp_path, "v.py")
    assert session.was_read("v.py") is True

    first = editor.apply_text_edit(
        str(tmp_path), "v.py", "value = 1", "value = 2", session=session
    )
    assert first.ok is True
    assert first.digest_relaxed is False
    assert first.digest_relaxed_reason == ""
    assert target.read_bytes() == b"value = 2\n"

    # A second edit in the same session is allowed: the mutation this session
    # applied is itself an observation, so the digest is re-baselined rather
    # than going stale against itself.
    second = editor.apply_text_edit(
        str(tmp_path), "v.py", "value = 2", "value = 3", session=session
    )
    assert second.ok is True, second.message
    assert target.read_bytes() == b"value = 3\n"
    assert session.revision("v.py") == second.post_sha256


def test_a_deliberate_relaxation_is_recorded_on_the_receipt(tmp_path: Path) -> None:
    write_bytes(tmp_path, "v.py", b"value = 1\n")

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "v.py",
        "value = 1",
        "value = 2",
        config={"require_edit_digest": False},
    )

    assert outcome.ok is True
    assert outcome.digest_required is False
    assert outcome.digest_relaxed is True
    assert "require_edit_digest disabled" in outcome.digest_relaxed_reason
    # The receipt, not the boolean, is what a consumer must be able to read.
    assert outcome.to_dict()["digest_relaxed_reason"]


def test_a_caller_that_relaxes_digest_still_cannot_escape_the_ambiguity_refusal(
    tmp_path: Path,
) -> None:
    # Relaxing ONE guard must not relax the others: this is the "one knob, one
    # behaviour" property, and it is what stops a deliberate relaxation from
    # quietly becoming a general off switch.
    body = b"x = 1\ny = 1\n"
    target = write_bytes(tmp_path, "dup.py", body)

    outcome = editor.apply_text_edit(
        str(tmp_path), "dup.py", "= 1", "= 2", config={"require_edit_digest": False}
    )

    assert outcome.error_kind == editor.ERROR_AMBIGUOUS_MATCH
    assert outcome.digest_relaxed is True
    assert target.read_bytes() == body


# --- vocabulary parity, encoding detection, atomic reuse ---------------------


def test_the_edit_refusal_slugs_are_the_kernels_slugs() -> None:
    # One vocabulary, machine-checked against the file this round does not own.
    assert editor.ERROR_AMBIGUOUS_MATCH == kernel_tools.ERROR_AMBIGUOUS_MATCH
    assert editor.ERROR_NO_MATCH == kernel_tools.ERROR_NO_MATCH
    assert editor.ERROR_STALE_READ == kernel_tools.ERROR_STALE_READ
    assert tools.EDIT_ERROR_AMBIGUOUS_MATCH == kernel_tools.ERROR_AMBIGUOUS_MATCH
    assert tools.EDIT_ERROR_NO_MATCH == kernel_tools.ERROR_NO_MATCH
    assert tools.EDIT_ERROR_STALE_READ == kernel_tools.ERROR_STALE_READ
    # And the kernel keeps ONE vocabulary: the three slugs this round reuses
    # are members of the kernel's own ERROR_* set, not a parallel set that
    # happens to share three names.
    kernel_slugs = {
        value
        for name, value in vars(kernel_tools).items()
        if name.startswith("ERROR_") and isinstance(value, str)
    }
    assert {
        kernel_tools.ERROR_AMBIGUOUS_MATCH,
        kernel_tools.ERROR_NO_MATCH,
        kernel_tools.ERROR_STALE_READ,
    } <= kernel_slugs


def test_every_edit_refusal_slug_the_editor_can_return_is_documented() -> None:
    vocabulary = tools.edit_refusal_vocabulary()
    assert set(vocabulary) | {tools.EDIT_ERROR_VALIDATION} == set(
        tools.EDIT_ERROR_KINDS
    )
    assert set(editor.EDIT_ERROR_KINDS) <= set(tools.EDIT_ERROR_KINDS)
    for meaning in vocabulary.values():
        assert meaning and meaning[0].islower()


def test_detect_encoding_resolves_bom_cookie_and_plain_utf8() -> None:
    assert editor.detect_encoding(b"x = 1\n") == "utf-8"
    assert editor.detect_encoding(b"\xef\xbb\xbfx = 1\n") == "utf-8-sig"
    assert (
        editor.detect_encoding(b"\xff\xfe\x00\x00" + "x".encode("utf-32-le"))
        == "utf-32"
    )
    assert editor.detect_encoding(b"\xff\xfe" + "x".encode("utf-16-le")) == "utf-16"
    # A cookie on the SECOND line is legal (PEP 263) and is honoured.
    assert (
        editor.detect_encoding(
            b"#!/usr/bin/env python\n# -*- coding: latin-1 -*-\nx = 'e'\n".replace(
                b"e'", b"\xe9'"
            )
        )
        == "iso8859-1"
    )
    # No declaration and not UTF-8: unknown, never guessed.
    assert editor.detect_encoding(b"x = '\xe9'\n") is None


def test_detect_newline_reports_the_dominant_convention_and_mixedness() -> None:
    assert editor.detect_newline(b"a\r\nb\r\n") == ("\r\n", False)
    assert editor.detect_newline(b"a\nb\n") == ("\n", False)
    assert editor.detect_newline(b"a\rb\r") == ("\r", False)
    assert editor.detect_newline(b"a\r\nb\n") == ("\r\n", True)
    assert editor.detect_newline(b"") == ("\n", False)


def test_the_write_goes_through_the_repository_atomic_write_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Requirement: reuse the existing primitive, do not invent a second one.
    import execution.workspace as workspace_module

    calls: list[tuple] = []
    real = workspace_module._atomic_write_bytes

    def spy(path, data, mode=None):
        calls.append((Path(path).name, bytes(data)))
        return real(path, data, mode)

    monkeypatch.setattr(workspace_module, "_atomic_write_bytes", spy)
    target = write_bytes(tmp_path, "v.py", b"value = 1\n")

    outcome = editor.apply_text_edit(
        str(tmp_path),
        "v.py",
        "value = 1",
        "value = 2",
        session=read_session(tmp_path, "v.py"),
    )

    assert outcome.ok is True
    assert [name for name, _ in calls] == ["v.py"]
    assert calls[0][1] == b"value = 2\n"
    assert target.read_bytes() == b"value = 2\n"

    # AGT-03: a default syntax failure is now caught BEFORE the write, so the
    # primitive is not called AT ALL. This is a STRONGER claim than "the
    # rollback went through the same primitive" - nothing was written, so there
    # is nothing to undo, and a `Path.write` is structurally unreachable.
    write_bytes(tmp_path, "w.py", b"def f():\n    return 1\n")
    before_calls = len(calls)
    refused = editor.apply_text_edit(
        str(tmp_path),
        "w.py",
        "    return 1\n",
        "    return 1(\n",
        session=read_session(tmp_path, "w.py"),
    )
    assert refused.rolled_back is True
    assert refused.pre_commit is True
    assert [name for name, _ in calls[before_calls:]] == []
    assert (tmp_path / "w.py").read_bytes() == b"def f():\n    return 1\n"

    # The ROLLBACK path still exists, for a caller-supplied `validate` gate -
    # whose `(root, [paths])` signature reads the tree, so it can only run
    # after the write. It is a SECOND call through the same primitive, never a
    # `Path.write`.
    write_bytes(tmp_path, "r.py", b"def f():\n    return 1\n")
    before_calls = len(calls)
    rolled = editor.apply_text_edit(
        str(tmp_path),
        "r.py",
        "    return 1\n",
        "    return 1 + 0\n",
        session=read_session(tmp_path, "r.py"),
        validate=lambda root, paths: (False, "the caller's own gate said no"),
    )
    assert rolled.rolled_back is True
    assert rolled.write_mode == "atomic_replace+rollback"
    assert [name for name, _ in calls[before_calls:]] == ["r.py", "r.py"]
    assert calls[-1][1] == b"def f():\n    return 1\n"


# --- the catalog entry point in harness/tools.py ------------------------------


def test_apply_catalog_edit_validates_then_applies_and_reports_the_slugs(
    tmp_path: Path,
) -> None:
    body = b"x = 1\ny = 1\n"
    write_bytes(tmp_path, "dup.py", body)

    refusal = tools.apply_catalog_edit(
        str(tmp_path),
        {"path": "dup.py", "old_string": "= 1", "new_string": "= 2"},
        config={"require_edit_digest": False},
    )
    assert refusal["error_kind"] == tools.EDIT_ERROR_AMBIGUOUS_MATCH
    assert len(refusal["candidates"]) == 2

    target = write_bytes(tmp_path, "one.py", b"z = 9\n")
    applied = tools.apply_catalog_edit(
        str(tmp_path),
        {"path": "one.py", "old_string": "z = 9", "new_string": "z = 10"},
        session=read_session(tmp_path, "one.py"),
    )
    assert applied["ok"] is True
    assert applied["error_kind"] == ""
    assert applied["write_mode"] == "atomic_replace"
    assert target.read_bytes() == b"z = 10\n"


def test_apply_catalog_edit_turns_a_schema_violation_into_a_validation_error(
    tmp_path: Path,
) -> None:
    receipt = tools.apply_catalog_edit(
        str(tmp_path), {"path": "one.py", "old_string": "a"}, session=None
    )
    assert receipt["ok"] is False
    assert receipt["error_kind"] == tools.EDIT_ERROR_VALIDATION
    assert "new_string" in receipt["message"]


def test_apply_catalog_edit_refuses_an_escaping_path_before_touching_disk(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"secret = 1\n")

    receipt = tools.apply_catalog_edit(
        str(tmp_path),
        {
            "path": "../outside.py",
            "old_string": "secret = 1",
            "new_string": "secret = 2",
        },
        config={"require_edit_digest": False},
    )

    assert receipt["ok"] is False
    assert outside.read_bytes() == b"secret = 1\n"


# --- every refusal leaves the file byte-for-byte unchanged --------------------


@pytest.mark.parametrize(
    "old_string,new_string,config",
    [
        ("nope\n", "x\n", {"require_edit_digest": False}),
        ("= 1", "= 2", {"require_edit_digest": False}),
        ("value = 1", "value = 2", {"require_edit_digest": True}),
        ("value = 1", "value = 2", {}),
        ("", "x", {"require_edit_digest": False}),
    ],
)
def test_no_refusal_ever_modifies_the_file(
    tmp_path: Path, old_string: str, new_string: str, config: dict
) -> None:
    target = write_bytes(tmp_path, "v.py", b"x = 1\ny = 1\nvalue = 1\n")
    before = target.read_bytes()

    outcome = editor.apply_text_edit(
        str(tmp_path), "v.py", old_string, new_string, config=config
    )

    assert outcome.ok is False
    assert outcome.error_kind in editor.EDIT_ERROR_KINDS
    assert target.read_bytes() == before
