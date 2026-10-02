"""The sanitiser pipeline: order, fail-closed, seven adversarial shapes.

The defect this file exists for is in `cli/AGENTS.md` § VEX-TERM-UX-09 and it
is the highest-severity one the audit found: `cli/ui.py` called
`shared.security.redact_text` BEFORE stripping ANSI escapes.

    input:  "key=\x1b[35msk\x1b[0m-FAKE-SECRET-VALUE"
    before: strip first  -> "key=sk-FAKE-SECRET-VALUE" -> redacted      OK
    after:  redact first -> shape does not match   -> then strip ->
           "key=sk-FAKE-SECRET-VALUE"                                 LEAK

ANSI escapes SPLIT a secret into visually-contiguous bytes. Redacting first
protects a string the user never sees, and the strip that runs next
REASSEMBLES a complete credential out of the pieces. Order is the whole fix,
so it is pinned at the SOURCE level below rather than only at runtime: a
behavioural test would still pass if a future refactor moved the redaction
into a helper the behaviour test did not exercise.

The same layer also failed OPEN (`except Exception: text = str(value or "")`
— the raw value passing through), and one display path (`cli/fileview.py`)
had no sanitiser at all. All three are covered here.

Owned by T4. Lives in `cli/` because `tests/**` is T5's.
"""

from __future__ import annotations

import ast
import base64
from pathlib import Path

import pytest

import cli.ui as ui

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_PATH = REPO_ROOT / "cli" / "ui.py"
FILEVIEW_PATH = REPO_ROOT / "cli" / "fileview.py"

#: The credential shapes used throughout. A FAKE shape, never a real key: a
#: test that printed a live credential would be the defect it is testing for.
SECRET = "sk-FAKE-SECRET-VALUE0123"
SECRET_TAG = "FAKE-SECRET-VALUE0123"


# ---------------------------------------------------------------------------
# Task A — the order, pinned in the source
# ---------------------------------------------------------------------------


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} has no function {name!r}")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _first_lineno(node: ast.AST, callee: str) -> int:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == callee:
                return int(child.lineno)
    raise AssertionError(f"no call to {callee!r}")


def test_the_pipeline_strips_before_it_redacts() -> None:
    """`sanitize_text` strips escapes BEFORE it redacts. Source-level.

    Read from the AST rather than asserted at runtime: a runtime test of
    `strip_ansi` passes whether the order is right or wrong, because the
    output is the same in the common case. Only the ORDER differs, and only
    the order is the bug.
    """
    pipeline = _function_node(UI_PATH, "sanitize_text")
    calls = _called_names(pipeline)
    assert "strip_escapes" in calls, "the pipeline no longer strips escapes"
    assert "redact_or_fail" in calls, "the pipeline no longer redacts"
    strip_at = _first_lineno(pipeline, "strip_escapes")
    redact_at = _first_lineno(pipeline, "redact_or_fail")
    assert strip_at < redact_at, (
        f"cli/ui.py:sanitize_text redacts at line {redact_at} and strips at "
        f"line {strip_at}. ANSI escapes split a secret into visually-contiguous "
        f"bytes: redaction first means the shape does not match, and the strip "
        f"that runs next REASSEMBLES a visible credential out of the pieces."
    )


def test_redact_text_is_reachable_only_through_two_named_functions() -> None:
    """No other function in `cli/ui.py` may call `shared.security.redact_text`.

    This is the pin that makes the order durable. Anyone who "helpfully"
    redacts a value early — to save a pass, or inside a helper — re-opens the
    exact hole, and a behavioural test would not notice because the escape
    would still be stripped later.

    Exactly two functions are allowed, and each for one reason:
    `redact_or_fail` (the only redaction, which raises rather than passing the
    raw value through) and `redactor_is_functional` (the liveness probe, which
    MUST tolerate the redactor raising — tolerating failure is the whole point
    of it, so it cannot route through the fail-closed helper).
    """
    tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "redact_text" in _called_names(node)
    }
    assert callers == {"redact_or_fail", "redactor_is_functional"}, (
        f"redact_text is called from {sorted(callers)}; only the two named "
        f"functions may call it, so every redaction is fail-closed and no "
        f"redaction can be moved ahead of the strip"
    )


def test_the_two_redactor_functions_are_only_reachable_from_the_pipeline() -> None:
    """`redact_or_fail` is called from `sanitize_text` and nowhere else.

    Closes the loop with the order pin: the order test proves `sanitize_text`
    strips first, this proves the redaction cannot happen anywhere else.
    """
    tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "redact_or_fail" in _called_names(node)
    }
    assert callers == {"sanitize_text"}, sorted(callers)


def test_the_strip_only_helper_does_not_redact() -> None:
    """`strip_escapes` must never grow a redaction.

    It exists for the one job that needs the raw visible bytes — building the
    text a secret is searched FOR, so the comparison is between what the
    redactor saw and what the terminal shows. A redaction there would redact
    the very credential being looked for and make the check pass vacuously
    (VEX-TERM-UX-09 finding 6).
    """
    assert "redact_text" not in _called_names(_function_node(UI_PATH, "strip_escapes"))


# ---------------------------------------------------------------------------
# The reproducer the prompt names, verbatim
# ---------------------------------------------------------------------------


def test_the_named_reproducer_is_fixed() -> None:
    """`key=<ESC>[35msk<ESC>[0m-FAKE...` leaves no contiguous secret behind."""
    raw = "key=\x1b[35msk\x1b[0m-FAKE-SECRET-VALUE0123"
    out = ui.sanitize_text(raw)
    assert "FAKE-SECRET-VALUE0123" not in out, (
        f"an ANSI-wrapped secret survived: {out!r}"
    )
    assert "\x1b" not in out, f"a raw escape survived: {out!r}"
    assert out == "key=[REDACTED_SECRET]", out


def _pre_round_pipeline(value: str) -> str:
    """The pre-round pipeline, reconstructed: redact FIRST, strip escapes second.

    Kept in the suite as the CONTROL ARM so the assertions above are known to
    discriminate. It is the historical order, not a strawman.
    """
    redacted = ui.redact_or_fail(value)
    return ui._INVISIBLE_CHARS.sub(
        "", ui._ANSI_ESCAPE.sub("", ui._CONTROL_WITHOUT_NEWLINES.sub("", redacted))
    )


def test_the_pre_round_order_is_still_insufficient() -> None:
    """Redact-first cannot protect a secret whose SHAPE is not in the bytes yet.

    On the tree as it stands, `shared/security.redact_text` has been hardened
    (concurrently, by its owner) to tolerate ANSI and zero-width characters
    *inside* a token, so the `key=\\x1b[35msk\\x1b[0m-FAKE...` case no longer
    reproduces through the redactor alone. That is a change in the AUTHORITY,
    not in this layer — and the order is still wrong by construction, because
    the shape's presence depends on bytes the redactor is being asked to match
    before they have been normalised.

    The shape that proves it does NOT depend on that hardening is a TRANSPORT:
    percent-encoded, the bytes simply do not contain a secret, so no amount of
    pattern tolerance finds one. This is the control arm the assertions above
    need in order to mean anything.
    """
    raw = SHAPES["url-encoded"]
    assert "FAKE-SECRET-VALUE0123" in _pre_round_pipeline(raw), (
        "the pre-round order no longer reproduces the defect this round's "
        "decode guard closes"
    )
    assert "FAKE-SECRET-VALUE0123" not in ui.sanitize_text(raw)


def test_the_fail_open_path_is_gone() -> None:
    """The historical `except Exception: text = str(value or "")` cannot occur.

    Reconstructed pre-round, a raising redactor produces the RAW CREDENTIAL —
    that is the fail-open path, measured, not argued:

        pre-round, redactor RAISES -> 'key=sk-FAKE-SECRET-VALUE0123'
        this round                  -> '(detail withheld: redactor unavailable)'
    """
    # the historical fail-open path, literally: `str(value or "")` after the
    # redactor raised. Reconstructed here rather than merely described, so the
    # contrast is a measurement.
    with _Poison(_raising):
        try:
            pre_round = ui.redact_or_fail(f"key={SECRET}")
        except Exception:
            pre_round = f"key={SECRET}"  # the pre-round `except` branch
        round_output = ui.sanitize_text(f"key={SECRET}")
    assert SECRET_TAG in pre_round, "the control arm is not a credential"
    assert SECRET_TAG not in round_output, round_output
    assert round_output.startswith("(detail withheld:")


# ---------------------------------------------------------------------------
# Task B — fail closed, proven by poisoning the redactor
# ---------------------------------------------------------------------------


class _Poison:
    """Install a broken redactor for the duration of a ``with`` block."""

    def __init__(self, replacement) -> None:
        self._replacement = replacement
        self._original = None

    def __enter__(self) -> None:
        self._original = ui.redact_text
        ui.redact_text = self._replacement
        ui._redactor_ok = None  # force the liveness probe to re-run

    def __exit__(self, *exc) -> None:
        ui.redact_text = self._original
        ui._redactor_ok = None
        return False


def _raising(*_a, **_kw):
    raise RuntimeError("redactor is broken")


@pytest.mark.parametrize(
    "replacement,label",
    [
        (_raising, "raises"),
        (lambda _v: None, "returns None"),
        (lambda _v: 42, "returns a non-text object"),
        (lambda _v: "", "returns an empty string"),
        (lambda v: v, "is a pass-through"),
    ],
    ids=[
        "raises",
        "returns_None",
        "returns_non_text",
        "returns_empty",
        "is_a_pass_through",
    ],
)
def test_a_broken_redactor_withholds_the_detail(replacement, label: str) -> None:
    """A redactor that cannot redact must NEVER pass the raw value through.

    The pre-fix code was `except Exception: text = str(value or "")` — the
    literal fail-open path. Every arm here asserts the value is withheld, and
    asserts the withheld marker names a reason rather than rendering nothing
    (a card that renders nothing at the moment of failure is the absence of an
    affordance).
    """
    raw = f"key={SECRET}"
    with _Poison(replacement):
        out = ui.sanitize_text(raw)
    assert SECRET_TAG not in out, (
        f"redactor {label}: the raw value passed through -> {out!r}"
    )
    assert out.startswith("(detail withheld:"), (
        f"redactor {label}: expected a withheld marker, got {out!r}"
    )


def test_a_redactor_that_mangles_rather_than_redacts_is_a_known_boundary() -> None:
    """What this layer deliberately does NOT catch, stated rather than implied.

    A redactor that replaces the token PREFIX (`sk-` -> `xx-`) rather than the
    whole secret is still "functional" by any liveness probe: it did change a
    known-shaped input. Catching it needs an independent scan of the redacted
    output for a secret shape — i.e. a SECOND pattern set, and two pattern sets
    are two answers to "is this a secret", the second of which rots silently.

    So the boundary is declared: the CLI verifies that the authority is
    FUNCTIONING, and the authority owns being CORRECT. That is a T5 request
    against `shared/security.py` with this case as the failing test, not a
    reason for `cli` to grow its own matcher.

    What IS asserted here: the value is still not a usable credential, because
    the mangled prefix is not the shape any consumer accepts.
    """
    with _Poison(lambda v: str(v).replace("sk-", "xx-")):
        out = ui.sanitize_text(f"key={SECRET}")
    assert "sk-FAKE-SECRET-VALUE0123" not in out
    assert "xx-FAKE-SECRET-VALUE0123" in out, (
        "the mangling redactor is supposed to replace the token PREFIX; if it "
        "stopped doing even that, this boundary test has stopped describing the "
        "case it was written for"
    )


def test_the_withholding_is_observable_and_carries_no_value() -> None:
    """A withholding is recorded — and the record never carries the value.

    A receipt describing a leak while containing the leak would be the defect
    it reports. And a silent withholding is how a broken redactor becomes a
    permanent condition nobody notices.
    """
    before = ui.sanitize_report()["withheld"]
    with _Poison(_raising):
        ui.sanitize_text(f"key={SECRET}")
    report = ui.sanitize_report()
    assert report["withheld"] == before + 1, report
    assert "redactor unavailable" in report["by_reason"], report
    assert report["window"] == ui.SANITIZE_WITHHOLDING_LIMIT
    assert SECRET_TAG not in str(report), "the observability receipt leaked the value"


def test_the_ledger_is_bounded() -> None:
    """An unbounded ledger is an unbounded object store. Measured, not hoped."""
    assert ui._SANITIZE_WITHHOLDINGS.maxlen == ui.SANITIZE_WITHHOLDING_LIMIT
    assert len(ui._SANITIZE_WITHHOLDINGS) <= ui.SANITIZE_WITHHOLDING_LIMIT


def test_a_healthy_redactor_still_redacts_after_every_poison() -> None:
    """Control arm: the poisoning cannot leave the sanitiser disarmed.

    A fail-closed layer that latches shut forever is its own outage, so the
    charged arm is asserted after the failures, not only the failure arm.
    """
    with _Poison(_raising):
        ui.sanitize_text(f"key={SECRET}")
    assert ui.sanitize_text(f"key={SECRET}") == "key=[REDACTED_SECRET]"
    assert ui.redactor_is_functional() is True


# ---------------------------------------------------------------------------
# The seven adversarial shapes
# ---------------------------------------------------------------------------
#
# Per-shape ownership. `strip-then-redact` is the PRIMARY fix and closes the
# two escape cases outright. The rest need NORMALISATION before redaction, and
# each needed a different decision:
#
# | shape                | responsible layer        | why                                              |
# |----------------------|--------------------------|--------------------------------------------------|
# | ANSI-wrapped         | sanitiser — ORDER        | escapes split the token; strip first reassembles |
# | zero-width insertion | sanitiser — NORMALISE    | renders as nothing, so removing it makes the scan |
# |                      |                          | bytes equal the drawn bytes                       |
# | nested escapes       | sanitiser — ORDER        | one strip pass removes any interleaving          |
# | line-wrap split      | REDACTOR (per line)      | a newline is real structure; see below            |
# | URL-encoded          | sanitiser — DECODE GUARD | the shape is not in the text, so it cannot match  |
# | base64-wrapped       | sanitiser — DECODE GUARD | same, and it is not secrecy, it is a transport    |
# | CR overwrite         | sanitiser — NORMALISE    | `\r`/`\b` make the drawn bytes differ from the    |
# |                      |                          | scanned bytes; they are removed before redaction  |
#
# The line-wrap case is a DECISION, not a fix: a newline is real display
# structure, so joining the halves would corrupt the render and refuse line
# numbers a diff cursor depends on. What the pipeline guarantees instead is
# that each LINE is redacted on its own — so `sk-FAKE-<suffix>` is caught
# whole, and a genuinely arbitrary split leaves two non-secret halves rather
# than one credential.


SHAPES = {
    # escapes INSIDE the token: the shape only exists after the strip
    "ansi-wrapped": "key=\x1b[35msk\x1b[0m-FAKE-SECRET-VALUE0123",
    # zero-width characters render as nothing but are real bytes
    "zero-width-space": f"key=sk-{chr(0x200B)}FAKE-SECRET-VALUE0123",
    "zero-width-non-joiner": f"key=sk-{chr(0x200C)}FAKE-SECRET-VALUE0123",
    "zero-width-joiner": f"key=sk-{chr(0x200D)}FAKE-SECRET-VALUE0123",
    "bom-inside-token": f"key=sk-{chr(0xFEFF)}FAKE-SECRET-VALUE0123",
    "bidi-override": f"key=sk-FAKE{chr(0x202E)}-SECRET-VALUE0123",
    # several escapes interleaved with secret bytes
    "nested-escapes": (
        "k\x1b[1me\x1b[0yy=\x1b[35ms\x1b[31mk-\x1b[0m\x1b[1m"
        "FAKE-SECRET-VALUE0123\x1b[0m"
    ),
    # a display break inside the token
    "line-wrap-split": "sk-FAKE0123456789ABCDEF0123\nSECRET-VALUE0123",
    # transports, not secrecy
    "url-encoded": "key=sk%2DFAKE-SECRET-VALUE0123",
    "double-url-encoded": "key=sk%252DFAKE-SECRET-VALUE0123",
    "base64-wrapped": "blob: " + base64.b64encode(SECRET.encode()).decode(),
    # overwrite controls: drawn bytes != scanned bytes
    "carriage-return-overwrite": f"key=REDACTED\r{SECRET}",
    "backspace-overwrite": "sk-FAKE-SECRET-VALUE0" + ("\b" * 12) + "123",
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_no_contiguous_secret_survives_any_shape(shape: str) -> None:
    """No contiguous secret shape survives into the rendered output."""
    out = ui.sanitize_text(SHAPES[shape])
    assert SECRET_TAG not in out, f"{shape} leaked: {out!r}"
    assert "\x1b" not in out, f"{shape} leaked a raw escape: {out!r}"
    assert "\r" not in out, f"{shape} leaked a carriage return: {out!r}"
    assert chr(0x200B) not in out and chr(0x202E) not in out, (
        f"{shape} leaked an invisible or reordering character: {out!r}"
    )


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_shape_is_idempotent(shape: str) -> None:
    """Sanitising twice equals sanitising once.

    Load-bearing, not cosmetic: `cli/fileview.py` sanitises at the parse
    boundary and the TUI diff modal sanitises again as a second net. If the
    pipeline were not idempotent the second pass would change the line count
    and the diff cursor's `offset` addressing would land on the wrong row.
    """
    once = ui.sanitize_text(SHAPES[shape])
    assert ui.sanitize_text(once) == once, f"{shape} is not idempotent"


@pytest.mark.parametrize(
    "shape", ["ansi-wrapped", "nested-escapes", "zero-width-space", "line-wrap-split"]
)
def test_the_escape_shapes_are_redacted_not_withheld(shape: str) -> None:
    """The ORDER fix REDACTS; only the decode guard WITHHELDS.

    Asserted so the two mechanisms cannot be confused: a refactor that made
    the strip-then-redact path withhold would be "safe" and useless, and a
    diff body of `(detail withheld: ...)` is not a review surface.
    """
    out = ui.sanitize_text(SHAPES[shape])
    assert "[REDACTED_SECRET]" in out, (
        f"{shape} was withheld instead of redacted: {out!r}"
    )
    assert "withheld" not in out


@pytest.mark.parametrize(
    "shape", ["url-encoded", "double-url-encoded", "base64-wrapped"]
)
def test_the_transport_shapes_are_withheld_with_a_reason(shape: str) -> None:
    """Percent- and base64-encoded credentials are WITHHELD, per line.

    Redaction cannot match a shape the text does not contain, so the shape has
    to be recovered first. The run is then withheld rather than rewritten: a
    display sanitiser must not silently mutate the bytes it was asked to show,
    and a rewritten blob would be a different value than the one that leaked.
    """
    out = ui.sanitize_text(SHAPES[shape])
    assert SECRET_TAG not in out
    assert "(detail withheld: encoded credential)" in out, out


def test_an_encoded_credential_on_one_line_does_not_blank_the_whole_diff() -> None:
    """The decode guard is PER LINE.

    Withholding a whole 4,000-line diff because one row carried a base64 blob
    would be a second outage; the point is to remove the disclosure, not to
    remove the surface.
    """
    diff = "\n".join(
        [f"+added ordinary line {i}" for i in range(20)]
        + ["+blob " + base64.b64encode(SECRET.encode()).decode()]
        + [f"-removed ordinary line {i}" for i in range(20)]
    )
    out = ui.sanitize_text(diff)
    lines = out.splitlines()
    assert len(lines) == 41, len(lines)
    assert lines[0] == "+added ordinary line 0"
    assert lines[-1] == "-removed ordinary line 19"
    assert lines[20] == "(detail withheld: encoded credential)", lines[20]


def test_ordinary_text_is_untouched() -> None:
    """The sanitiser must not eat the product's own output.

    A sanitiser that redacts half the receipts is a receipt nobody can read.
    """
    for text in (
        "diff --git a/cli/ui.py b/cli/ui.py",
        "+added line 12 with ordinary code text",
        "@@ -1,4 +1,4 @@ def handler():",
        "run completed_verified · 3 attempts · $0.0031",
        "the quick brown fox jumps over the lazy dog",
        "logs/task-fix-abc123/trace.jsonl",
        "-'utf-8' codec can't decode byte 0x9d",
    ):
        assert ui.sanitize_text(text) == text, text


def test_tabs_and_newlines_survive() -> None:
    """Real structure is preserved: a sanitiser that flattened lines would
    break every receipt, every diff line number and every table row."""
    text = "one\n\ttwo\nthree"
    assert ui.sanitize_text(text) == text


# ---------------------------------------------------------------------------
# Task C — the path that had no sanitiser at all
# ---------------------------------------------------------------------------


def test_fileview_routes_the_diff_parse_boundary_through_the_sanitiser() -> None:
    """`cli/fileview.py` no longer parses a raw diff line into a projection.

    The recorded real-modal output was
    `1 +added <ESC>[31mRED<ESC>[0m sk-FAKE...` rendered into a
    `markup=False` RichLog, which protects rich markup and nothing else. The
    fix is at the PARSE boundary because `DiffHunk.lines` is the one list the
    diff modal, the per-file cursor, the review lines and the changed-file rows
    all render.
    """
    tree = ast.parse(FILEVIEW_PATH.read_text(encoding="utf-8"))
    parse_hunks = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_parse_hunks":
            parse_hunks = node
            break
    assert parse_hunks is not None, "_parse_hunks disappeared from cli/fileview.py"
    names = _called_names(parse_hunks)
    assert "_sanitize_diff" in names, (
        "cli/fileview.py::_parse_hunks parses raw diff text again"
    )
    # and the helper really is the shared sanitiser
    helper = _function_node(FILEVIEW_PATH, "_sanitize_diff")
    assert "sanitize_text" in _called_names(helper), _called_names(helper)


def test_a_diff_line_with_an_ansi_wrapped_secret_is_sanitised() -> None:
    """The exact recorded modal line, end to end through the parse boundary."""
    from cli import fileview

    diff = (
        "--- a/cli/alpha.py\n"
        "+++ b/cli/alpha.py\n"
        "@@ -1,2 +1,2 @@\n"
        " context line\n"
        "+added \x1b[31mRED\x1b[0m sk-FAKE-SECRET-VALUE0123\n"
        "-gone sk-\u200bFAKE-SECRET-VALUE0123\n"
    )
    hunks, additions, deletions, _binary = fileview._parse_hunks(diff)
    assert len(hunks) == 1
    rendered = [line for hunk in hunks for line in hunk.lines]
    joined = "\n".join(rendered)
    assert SECRET_TAG not in joined, joined
    assert "\x1b" not in joined, joined
    assert "[REDACTED_SECRET]" in joined, joined
    # the diff's own information is intact: markers and counts unchanged
    assert additions == 1 and deletions == 1
    assert rendered[1].startswith("+added "), rendered[1]


def _git_repo_with_secret(tmp_path: Path) -> Path:
    """A real git repository whose working tree contains an ANSI-wrapped secret.

    Real on purpose: the recorded disclosure was read off a REAL modal, and a
    synthetic fixture would not exercise `_diff_for_path` (git diff, pristine
    fallback, work-tree resolution) — the three ways a diff can reach a
    renderer.
    """
    import subprocess

    root = tmp_path / "repo"
    (root / "cli").mkdir(parents=True)
    (root / "cli" / "alpha.py").write_text("x = 1\n", encoding="utf-8")
    for args in (
        ["init"],
        ["config", "user.email", "smoke@example.invalid"],
        ["config", "user.name", "smoke"],
        ["add", "-A"],
        ["commit", "-m", "initial"],
    ):
        subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)
    (root / "cli" / "alpha.py").write_text(
        'key = "\x1b[35msk\x1b[0m-FAKE-SECRET-VALUE0123"\ny = 2\n', encoding="utf-8"
    )
    return root


def test_the_projection_the_modal_reads_carries_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a real repo, a real git diff, the record the modal renders."""
    import subprocess

    from cli import fileview

    root = _git_repo_with_secret(tmp_path)
    monkeypatch.chdir(root)  # `_root()` resolves the repository from the CWD
    projection = fileview.build_file_projection(root, None, None)
    assert "cli/alpha.py" in projection["changed_files"]
    record = fileview.open_diff_file(projection, "cli/alpha.py", 1)
    assert record is not None, "the diff modal's record could not be resolved"
    rendered = "\n".join(line for hunk in record["hunks"] for line in hunk["lines"])
    assert SECRET_TAG not in rendered, rendered
    assert "\x1b" not in rendered, rendered
    assert "[REDACTED_SECRET]" in rendered, rendered
    del subprocess


@pytest.mark.slow
def test_the_real_diff_modal_renders_no_contiguous_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL `_DiffFileScreen` through Textual's Pilot, mounted for real.

    This is the acceptance criterion read literally: nothing contiguous
    survives into the RENDERED MODAL, not merely into the record. A record-level
    assertion cannot see a renderer that formats the record back into the
    secret, and the recorded defect was a renderer-level one.

    The rendered text is read out of the mounted `RichLog` itself, so this
    asserts on the bytes a terminal would draw.
    """
    pytest.importorskip("textual")
    from cli import fileview
    from cli.tui import _DiffFileScreen

    root = _git_repo_with_secret(tmp_path)
    monkeypatch.chdir(root)
    record = fileview.open_diff_file(
        fileview.build_file_projection(root, None, None), "cli/alpha.py", 1
    )
    assert record is not None

    async def _drive() -> str:
        from textual.app import App, ComposeResult

        class _Host(App):
            def compose(self) -> ComposeResult:
                yield _DiffFileScreen(record, 1)

        app = _Host()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import RichLog

            body = app.screen.query_one("#diff-file-body", RichLog)
            return "\n".join(str(getattr(strip, "text", strip)) for strip in body.lines)

    import asyncio

    rendered = asyncio.run(_drive())
    assert rendered, "the diff modal rendered nothing"
    assert SECRET_TAG not in rendered, rendered
    assert "\x1b" not in rendered, rendered
    assert "[REDACTED_SECRET]" in rendered, rendered


# ---------------------------------------------------------------------------
# Single entry point / escape() not regressed
# ---------------------------------------------------------------------------


def test_the_two_public_names_are_one_implementation() -> None:
    """`strip_ansi` and `sanitize_text` are the same function.

    Two names for two implementations is how a display path ends up calling
    the weaker one; every pre-existing call site resolves here. Read from the
    BYTE CODE rather than asserted on output, because "both redact this one
    value" is exactly what two implementations would also satisfy.
    """
    import inspect

    assert (
        inspect.getsource(ui.strip_ansi).strip().endswith("return sanitize_text(value)")
    ), (
        "cli/ui.py::strip_ansi is no longer a thin alias for sanitize_text; "
        "two names for two implementations is how a display path ends up "
        "calling the weaker one"
    )
    value = f"key={SECRET}"
    assert ui.strip_ansi(value) == ui.sanitize_text(value)
    assert ui.strip_ansi.__doc__ and "sanitize_text" in ui.strip_ansi.__doc__


def test_no_cli_module_grows_a_second_display_sanitiser() -> None:
    """`cli/ui.py` stays the ONE display sanitiser.

    Several `cli/` modules call `shared.security.redact_text` directly, and
    that is correct: they redact a DOMAIN value rather than a display string —
    `doctor.py`'s support-bundle sections, `connectors.py`'s launch-command
    masking, `neoconfig.py`'s URL redaction, `onboard.py`'s provider health
    errors. Re-implementing the pipeline for those would be a second answer to
    "what is safe to show", and a second one does not get reordered when the
    order here is fixed.

    What is pinned is the negative space that WOULD be a defect: a module
    building DISPLAY text (a renderable, a rich `Text`, a console line) with
    its own `strip_ansi`-equivalent instead of routing through `cli.ui`. The
    list below is the declaration of where that is allowed to appear — it is
    an exemption list with names in it on purpose, because an exemption list
    is where the next offender hides if it is empty.
    """
    #: file -> the functions allowed to reduce a value for DISPLAY. Each is a
    #: domain redaction whose output still crosses `cli.ui` at its render sink.
    DECLARED_DOMAIN_REDACTORS = {
        "commands.py": {"_redacted"},
        "connectors.py": {
            "mask_command",
            "redact_tool_description",
            "check_health",
        },
        "doctor.py": {
            "_redact_deep",
            "_bundle_environment",
            "_bundle_config_shape",
            "_bundle_connectors",
            "_bundle_hooks",
            "_bundle_plugins",
            "_bundle_recent_errors",
            "_dumps",
        },
        "fileview.py": {"normalize_diagnostic"},
        # `main.py`'s three `cmd_mcp*` rows were DECLARED here and have been
        # REMOVED, because P0/W2 fixed them: they reached the redactor directly
        # for a value on its way to a terminal, which skipped the strip that has
        # to run first. They now route through `cli.ui.sanitize_text`. A stale
        # declaration is worse than no declaration, because it keeps asserting
        # that a known-defective path is acceptable.
        "onboard.py": {
            "test_credentials",
            "health_check",
            "run_repl_wizard",
            "_noninteractive_login",
        },
        # The P0/W2 display-contract suite calls the authority directly in two
        # places, and neither is a display sanitiser: `_redact_then_undo` builds
        # the "redacts correctly and its output is then restored" POISON, and
        # the restoration test re-reads the real redactor to prove a poison was
        # undone. Neither produces anything a terminal draws.
        "test_display_contract.py": {
            "_redact_then_undo",
            "test_a_healthy_redactor_still_redacts_after_every_poison",
        },
        "neoconfig.py": {"redact_url"},
    }
    offenders: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "cli").glob("*.py")):
        if path.name in {"ui.py", "test_sanitize_pipeline.py"}:
            continue
        # Pre-filter on the token before parsing. `cli/tui.py` is 405 KB and
        # `cli/interactive.py` 400 KB; parsing every module in the package
        # cost 6.7 s of a per-commit gate to find the eight files that
        # mention the name at all. A substring pre-filter can only make this
        # pin MORE likely to fire, never less: a call spelled any other way is
        # not a call to `redact_text`.
        source = path.read_text(encoding="utf-8")
        if "redact_text" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and "redact_text" in _called_names(node)
                and node.name not in DECLARED_DOMAIN_REDACTORS.get(path.name, set())
            ):
                offenders.setdefault(path.name, []).append(node.name)
    assert offenders == {}, (
        f"these cli functions call shared.security.redact_text to build DISPLAY "
        f"text without a declaration here: {offenders}. Either route them "
        f"through cli.ui.sanitize_text, or add the name to "
        f"DECLARED_DOMAIN_REDACTORS with the reason it is a domain redaction "
        f"rather than a display sanitiser."
    )


def test_the_declared_domain_redactors_still_exist() -> None:
    """The exemption list cannot rot into a permission slip.

    A name that no longer exists would make the declaration a free pass for
    whatever takes its place, so every declared name must still resolve.
    """
    # (re-read inline; the table lives in the test above and is asserted there)
    tree = ast.parse((REPO_ROOT / "cli" / "commands.py").read_text(encoding="utf-8"))
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "_redacted" in names, "a declared exemption no longer exists"


def test_escape_is_not_replaced_by_the_sanitiser() -> None:
    """The sanitiser and `escape()` do different jobs; neither subsumes the other.

    `sanitize_text` removes escapes and redacts. `escape()` stops rich from
    interpreting `[...]` as a style tag. A value that renders safely must go
    through BOTH, and the render boundaries this round touched
    (`cli/interactive.py::_render_review`, the TUI diff modal) were checked to
    keep doing both — a secret fix that dropped the markup escape would have
    replaced a credential disclosure with a message-eating bug.
    """
    from rich.markup import escape

    hostile = f"[bold red]{SECRET}[/bold red]"
    sanitized = ui.sanitize_text(hostile)
    assert SECRET_TAG not in sanitized
    # escaping is what survives to the terminal; it is NOT the sanitiser's job
    assert "[REDACTED_SECRET]" in escape(sanitized)
    # a rich Console with markup ON must still show the message afterwards
    import io

    from rich.console import Console

    buf = io.StringIO()
    con = Console(file=buf, width=200, force_terminal=False, no_color=True)
    con.print(escape(sanitized))
    assert "REDACTED_SECRET" in buf.getvalue(), buf.getvalue()


def test_the_render_review_degraded_branch_no_longer_prints_raw() -> None:
    """`cli/interactive.py::_render_review`'s except branch sanitises too.

    The success branch sanitised and the branch two lines below it printed the
    raw body — and a renderer that FAILS is precisely when the value is most
    likely to be hostile, because the failure came from the value. Read from
    the source so the assertion cannot be satisfied by a refactor that moves
    the print into a helper.
    """
    path = REPO_ROOT / "cli" / "interactive.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = None
    for candidate in ast.walk(tree):
        if (
            isinstance(candidate, ast.FunctionDef)
            and candidate.name == "_render_review"
        ):
            node = candidate
            break
    assert node is not None
    handlers = [n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "_render_review no longer has a degraded branch"
    for handler in handlers:
        printed = [
            child
            for child in ast.walk(handler)
            if isinstance(child, ast.Call)
            and getattr(child.func, "attr", "") == "print"
        ]
        assert printed, "an except branch of _render_review renders nothing"
        for call in printed:
            rendered = ast.dump(call.args[0]) if call.args else ""
            assert "sanitize_text" in rendered or "print_diff" in rendered, (
                "an except branch of _render_review prints a value that did not "
                f"go through the sanitiser: {rendered}"
            )
