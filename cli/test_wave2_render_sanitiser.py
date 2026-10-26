"""T4.W2.2 - the sanitiser contract, proved on the ACTUAL rendered bytes.

**Why this file is not a unit test.** Wave 1 added render paths that display
FILE CONTENT - the diff pane, the per-hunk view, the review modal, the
rail. That is the exact class that leaked in P0, and the class is only closed
if the check is made on what the product DRAWS, not on what a renderer
returns. So:

* every payload below is written into a REAL file in a REAL git repository,
  a REAL run journal is written beside it, and a REAL unified diff is
  generated;
* the payload is then rendered through the REAL `cli.review` document, the
  REAL `_ReviewScreen`'s `RichLog`, and the REAL REPL `Console`;
* the assertion is made against the concatenated RENDERED STRIP TEXT.

**The three payloads**, one per row of the brief, and each is the shape the
brief names:

| payload | shape | why it is hard |
|---|---|---|
| `ansi-wrapped` | ANSI escapes INSIDE a credential | redacting before stripping redacts a string the reader never sees, and the strip then REASSEMBLES a live credential out of the pieces |
| `zero-width-split` | U+200B and friends between every character | renders as nothing, so "the bytes the redactor scanned" and "the bytes the terminal draws" differ |
| `cr-overwrite` | a `\\r` inside the line | hides what precedes it on a real terminal; removing `\\r` outright GLUES `key=REDACTED` onto the secret and destroys the token boundary, which is why `\\r` becomes a SPACE and `\\b` becomes nothing |

**What "contiguous" means here.** The assertion is not "the secret is absent
as a substring of the source file" - that is true before any sanitiser runs,
because the payloads are stored escaped. It is that no rendering of the
payload contains the secret in any contiguous run, after which the INVISIBLE
characters are removed from the rendered output - i.e. after reconstructing
what a terminal would actually put on the glass, in order.

Host-only: no Docker, no provider, no network, no credential. Every payload
is a hardcoded fake shape.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

CLI = Path(__file__).resolve().parent
REPO = CLI.parent

#: The one fake credential shape. It is deliberately long and deliberately
#: fake; nothing here ever reads a real store.
SECRET = "sk-FAKE-SECRET-VALUE0123456789"

#: What a terminal renders as NOTHING. Used to reconstruct "what the user
#: actually sees" from the raw render, which is the only way a zero-width
#: split can be caught: each half is harmless alone.
INVISIBLE = "".join(
    chr(c)
    for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x180E)
)


def _splayed(value: str) -> str:
    """Every character of ``value``, separated by a zero-width space."""
    return INVISIBLE.join(value)


PAYLOADS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "ansi-wrapped",
        "why": (
            "ANSI escapes INSIDE the credential. Redacting first protects a "
            "string the reader never sees and the strip that runs next "
            "reassembles a live credential out of the pieces - the exact "
            "defect P0 found in cli/ui.py's pipeline order."
        ),
        "line": f"KEY = \"\x1b[35m{SECRET[:8]}\x1b[0m{SECRET[8:]}\"",
    },
    {
        "name": "zero-width-split",
        "why": (
            "A zero-width space between every character. It renders as "
            "nothing, so each half is harmless on its own and only the "
            "reassembled form is the credential."
        ),
        "line": f"KEY = '{_splayed(SECRET)}'",
    },
    {
        "name": "cr-overwrite",
        "why": (
            "A real carriage return (0x0D) inside the line, after an "
            "ESC[0D cursor-home. On a real terminal it moves the cursor to "
            "column 0 so the text after it OVERWRITES what precedes it. "
            "Replacing it with a SPACE is what makes the token visible to the "
            "redactor; DELETING it instead glues the prefix onto the secret, "
            "destroys the token boundary and lets the credential survive in "
            "full - which is exactly what this payload proved on the review "
            "surface before the boundary stopped doing it itself."
        ),
        # chr(13) is the REAL byte. A literal backslash-r two-character
        # sequence is a different payload (a value that merely CONTAINS the
        # text "\r"), and the first version of this table used it and
        # therefore tested nothing about carriage returns at all.
        "line": 'KEY = "key=SECRET\x1b[0D' + chr(13) + SECRET + '"',
    },
)


# ---------------------------------------------------------------------------
# A real repository, a real run, a real diff.
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace"))


def _write(root: Path, name: str, data: bytes) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def build_fixture(root: Path, payloads: Tuple[Dict[str, Any], ...]) -> Dict[str, Any]:
    """A real repo, a real pre-image, a real journal, and a real edit.

    The payload goes in as an ADDED line, so it appears in the unified diff
    the review surface renders. `pristine/` holds the pre-image so the
    review's own hash check passes and the file lands in the change set
    rather than being refused as a concurrent edit.
    """
    import hashlib

    repo = root / "repo"
    task = root / "logs" / "fix-adversarial"
    name = "settings.py"
    base = b"# settings\nVALUE = 1\n"
    after = b"# settings\nVALUE = 1\n" + b"".join(
        (p["line"] + "\n").encode("utf-8") for p in payloads
    )

    _write(repo, name, base)
    _write(repo, ".gitignore", b"__pycache__/\n")
    _write(task / "pristine", name, base)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "adversarial@example.invalid")
    _git(repo, "config", "user.name", "adversarial")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")

    _write(repo, name, after)
    rows = [
        {"event": "task_start", "payload": {"task_id": task.name}},
        {
            "event": "edit_applied",
            "payload": {
                "path": name,
                "pre_sha256": hashlib.sha256(base).hexdigest(),
                "post_sha256": hashlib.sha256(after).hexdigest(),
            },
        },
        {
            "event": "result",
            "payload": {
                "status": "completed_verified",
                "attempts": 1,
                "model_calls": 1,
                "evidence": [
                    {
                        "kind": "target",
                        "command": "pytest",
                        "status": "passed",
                        "flaky": False,
                    }
                ],
            },
        },
    ]
    with (task / "trace.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return {"repo": repo, "task": task, "name": name, "after": after}


# ---------------------------------------------------------------------------
# The render paths. Each returns the bytes a real surface would draw.
# ---------------------------------------------------------------------------


def rendered_by_review_lines(fixture: Dict[str, Any]) -> str:
    """`cli.review.review_lines` - the plain review receipt, the REPL's path.

    EXPANDED, deliberately. The collapsed roster renders a file name and a
    verdict but NOT the diff body, so a gate over the collapsed render would
    pass on a surface that never showed the payload at all - which is exactly
    how this round's first probe reported "LEAK=False" for a render that
    contained no diff. The expanded form is the one a user reaches with
    `/diff <file>`, and it is the one that carries file CONTENT.
    """
    from cli import review

    document = review.build_review(
        fixture["task"], fixture["repo"], width=120, include_diff_lines=True
    )
    name = fixture["name"]
    rendered = review.render_review(document, width=120, expanded=[name])
    return "\n".join(review.review_lines(rendered, expanded=[name]))


def rendered_by_review_text(fixture: Dict[str, Any]) -> str:
    """`cli.review.review_text_lines` - the rich `Text` path the modal uses."""
    from cli import review

    document = review.build_review(
        fixture["task"], fixture["repo"], width=120, include_diff_lines=True
    )
    name = fixture["name"]
    rendered = review.render_review(document, width=120, expanded=[name])
    return "\n".join(
        str(line) for line in review.review_text_lines(rendered, expanded=[name])
    )


def rendered_by_tui_modal(fixture: Dict[str, Any]) -> str:
    """The REAL `_ReviewScreen`, mounted, with its own `RichLog` read back.

    This is the surface the brief is about: file content reaches a terminal
    through this widget, so this is the render that has to be clean.

    TWO lessons are baked in, both paid for by the first version of this
    probe. The screen is mounted DIRECTLY rather than reached through
    `NeoApp._handle_line`, because a `RichLog`'s queued writes are only
    flushed by the widget's own mount and reading `body.lines` off a screen
    pushed onto an app that is still settling returns twelve EMPTY strips -
    a driver reporting "the modal renders nothing" about a modal rendering
    perfectly. And the read is `strip.text`, because a `Strip` joined
    segment-by-segment by `str(segment)` yields `Strip([...])`, which is how
    the same probe reported zero rows a second time.
    """
    import asyncio

    from textual.app import App, ComposeResult
    from textual.widgets import RichLog

    from cli import review

    name = fixture["name"]
    document = review.build_review(
        fixture["task"], fixture["repo"], width=120, include_diff_lines=True
    )
    rendered = review.render_review(document, width=120, expanded=[name])
    lines = review.review_lines(rendered, expanded=[name])

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield _tui._ReviewScreen(lines, title="review")

    async def drive() -> str:
        app = _Host()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            body = app.screen.query_one("#review-body", RichLog)
            return "\n".join(
                str(getattr(strip, "text", strip)) for strip in body.lines
            )

    from cli import tui as _tui

    return asyncio.run(drive())


def rendered_by_repl(fixture: Dict[str, Any]) -> str:
    """The REAL REPL console, with the real dispatcher, in a real repo."""
    from cli import interactive as _iv

    printed: List[str] = []

    class _Tee:
        """rich writes `str` and then calls `flush`; a stub with only
        `write` fails inside rich's own writer, which is a harness defect
        that reads as a product one."""

        def write(self, text: str) -> int:
            printed.append(text)
            return len(text)

        def flush(self) -> None:
            return None

        def isatty(self) -> bool:
            return False

    original = _iv.ui.console()
    buffer = _Tee()
    original._file = buffer  # type: ignore[attr-defined]
    try:
        state: Dict[str, Any] = {
            "repo": str(fixture["repo"]),
            "file_config": {},
        }
        last = {"task_id": fixture["task"].name}
        _iv._slash_command(
            f"/diff {fixture['name']}",
            "",
            last,
            fixture["task"].parent,
            state,  # type: ignore[arg-type]
        )
    finally:
        original._file = None  # type: ignore[attr-defined]
        _iv.ui.console.cache_clear()
    return "".join(printed)


#: name -> the render path. Every one is a REAL surface, not a function call
#: on a renderer in isolation.
RENDER_PATHS: Dict[str, Any] = {
    "cli.review.review_lines": rendered_by_review_lines,
    "cli.review.review_text_lines": rendered_by_review_text,
    "cli.tui._ReviewScreen RichLog": rendered_by_tui_modal,
    "cli.interactive REPL console": rendered_by_repl,
}


def _terminal_view(text: str) -> str:
    """Reconstruct what a terminal would DRAW, from the raw render.

    Two transformations, both of which the terminal performs:

    * ANSI escapes are stripped - a terminal does not draw them, but a
      credential SPLIT by them reassembles into a visible one, which is the
      whole point of the ``ansi-wrapped`` payload;
    * invisible codepoints are removed - a terminal draws nothing for them,
      so the credential they hide is the one on the glass.

    This is deliberately NOT ``cli.ui.sanitize_text``: that is the thing
    under test. Using it to decide what the user would see would let the
    sanitiser grade its own homework.
    """
    without_escapes = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
    for codepoint in INVISIBLE:
        without_escapes = without_escapes.replace(codepoint, "")
    return without_escapes.replace("\r", "")


# ---------------------------------------------------------------------------
# The pins.
# ---------------------------------------------------------------------------


class TestEveryAdversarialPayloadSurvivesNoRealRender:
    def test_the_fixture_actually_carries_the_payloads_into_the_diff(self) -> None:
        """If the fixture does not reach the renderer, the gates below are free.

        Asserted first, and in this order, because a sanitiser gate over an
        empty render is the vacuous pass this repo has a documented history
        of: three such gates were found by an audit that checked whether the
        evidence driver could reach its own subject.
        """
        import tempfile

        from cli import review

        root = Path(tempfile.mkdtemp()) / "adv"
        try:
            fixture = build_fixture(root, PAYLOADS)
            name = fixture["name"]
            document = review.build_review(
                fixture["task"], fixture["repo"], width=120
            )
            raw = "\n".join(
                review.review_lines(
                    review.render_review(document, width=120, expanded=[name]),
                    expanded=[name],
                )
            )
            # The RAW DIFF, before any sanitiser: this is what proves the
            # payload reaches the renderer. Checked on the FILE BYTES rather
            # than on the receipt, because the receipt is the thing under
            # test and a gate that reads the thing under test proves nothing.
            assert SECRET in _terminal_view(fixture["after"].decode("utf-8")), (
                "the payload is not in the file the diff is generated from"
            )
            assert raw.strip(), "the review receipt rendered nothing at all"
            assert fixture["name"] in raw, (
                "the receipt does not name the file, so it may not be showing "
                "that file's content either"
            )
            assert "@@" in raw, (
                "the receipt carries no hunk header, so the diff BODY was "
                "never rendered - every sanitiser assertion below would pass "
                "on a surface that showed no file content"
            )
            # And the payload must be in the render INPUT. This is asserted
            # on the file bytes above precisely because asserting it on the
            # receipt would require the leak to still be there; the receipt
            # is the thing under test and must not be the evidence.
            assert SECRET not in _terminal_view(raw), (
                "the receipt already shows the raw credential with no "
                "sanitiser involved - which means the fixture is bypassing "
                "the boundary rather than the boundary failing"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_no_render_path_shows_the_credential_contiguously(self) -> None:
        """THE pin. Three payloads x four REAL render paths."""
        import tempfile

        root = Path(tempfile.mkdtemp()) / "adv"
        try:
            fixture = build_fixture(root, PAYLOADS)
            findings: List[str] = []
            for path_name, renderer in RENDER_PATHS.items():
                rendered = renderer(fixture)
                if not rendered.strip():
                    findings.append(f"{path_name}: rendered NOTHING (vacuous)")
                    continue
                view = _terminal_view(rendered)
                if SECRET in view:
                    where = view.index(SECRET)
                    findings.append(
                        f"{path_name}: the credential is CONTIGUOUS at "
                        f"offset {where}: {view[max(0, where - 40):where + 50]!r}"
                    )
            assert not findings, (
                "a credential survived a real render:\n"
                + "\n".join(f"  {line}" for line in findings)
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_each_payload_is_actually_blocked_not_merely_absent(self) -> None:
        """A gate passes if the renderer prints nothing at all.

        The line the payload came from must still be VISIBLE and USABLE -
        redacted, bounded, and carrying the surrounding code - because a
        sanitiser that "blocks" a secret by deleting the whole line destroys
        the diff a user is trying to review, and that is the same defect in
        the opposite direction.
        """
        import tempfile

        root = Path(tempfile.mkdtemp()) / "adv"
        try:
            fixture = build_fixture(root, PAYLOADS)
            rendered = _terminal_view(rendered_by_review_lines(fixture))
            assert "VALUE = 1" in rendered, (
                "the diff lost its context line; a sanitiser that removes the "
                "line is not a sanitiser, it is a data loss"
            )
            assert rendered.count("KEY") >= len(PAYLOADS), (
                "the payload lines vanished rather than being redacted, so a "
                f"reader cannot tell a redacted value from a missing one: "
                f"{rendered.count('KEY')} KEY lines for {len(PAYLOADS)} payloads"
            )
            assert rendered.count("[REDACTED_SECRET]") >= len(PAYLOADS), (
                "each payload must come back REDACTED and marked; a value that "
                "disappears is indistinguishable from a value that was never "
                "there"
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_raw_render_never_carries_an_escape_byte(self) -> None:
        """No C0 control byte reaches the surface, terminal or not.

        Distinct from the contiguous check: a lone `\\x1b` with no credential
        around it is still a byte the terminal will interpret, and a
        credential check alone would not see it.
        """
        import tempfile

        root = Path(tempfile.mkdtemp()) / "adv"
        try:
            fixture = build_fixture(root, PAYLOADS)
            for path_name, renderer in RENDER_PATHS.items():
                rendered = renderer(fixture)
                bad = [
                    f"\\x{ord(ch):02x}"
                    for ch in rendered
                    if ord(ch) < 32 and ch not in "\n\r\t"
                ]
                assert not bad, f"{path_name}: raw control bytes {sorted(set(bad))}"
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestTheOrderIsPinnedInTheSource:
    """Strip, THEN redact. The order is the whole fix and it is invisible
    behaviourally on a tree whose authority has grown tolerant of ANSI inside
    a token - which is exactly the tree this repo is on."""

    def _calls(self, module: str, function: str) -> List[str]:
        tree = ast.parse((CLI / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                return [
                    child.func.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                ]
        raise AssertionError(f"{module}::{function} is gone")

    def test_the_sanitiser_strips_before_it_redacts(self) -> None:
        calls = self._calls("ui.py", "sanitize_text")
        strip_at = next(
            (i for i, name in enumerate(calls) if name == "strip_escapes"), None
        )
        redact_at = next(
            (i for i, name in enumerate(calls) if name == "redact_or_fail"), None
        )
        assert strip_at is not None, "sanitize_text no longer strips escapes"
        assert redact_at is not None, "sanitize_text no longer redacts"
        assert strip_at < redact_at, (
            f"sanitize_text redacts (call {redact_at}) before it strips (call "
            f"{strip_at}); redacting first protects a string the reader never "
            "sees and the strip then reassembles a live credential"
        )

    def test_the_review_boundary_strips_before_it_redacts(self) -> None:
        """`cli.review._bound` is where EVERY rendered review line passes.

        P0's audit named this as the model: sanitise at the boundary the
        content crosses, not at the place it is printed, because a per-call-site
        list is a list that can be short. The order assertion lives here
        because the review module's own sanitiser is a SEPARATE function from
        the pipeline above.
        """
        node = None
        for candidate in ast.walk(ast.parse((CLI / "review.py").read_text("utf-8"))):
            if isinstance(candidate, ast.FunctionDef) and candidate.name == "_bound":
                node = candidate
                break
        assert node is not None, "review._bound is gone"
        body = ast.unparse(node)
        sanitize_pos = body.find("_sanitizer()")
        assert sanitize_pos != -1, "review._bound no longer calls a sanitiser"
        # The regression this round fixed, asserted as an ABSENCE. The
        # boundary used to `replace("\r", " ")` BEFORE redacting, which
        # destroyed the token boundary and let a cr-overwrite payload render
        # the credential in full. `cli.ui.sanitize_text` owns that
        # substitution; a second one here is a second answer to the same
        # question and this one was wrong.
        assert 'replace("\\r"' not in body, (
            "review._bound rewrites a carriage return BEFORE redacting; that "
            "splits the credential at the boundary the redactor needs"
        )
        assert ".replace(" not in body.split("_sanitizer()")[0], (
            "review._bound rewrites the text before handing it to the "
            "sanitiser; any substitution here can destroy the token boundary "
            "the redactor matches on"
        )

    def test_every_render_path_this_round_added_reaches_the_sanitiser(self) -> None:
        """The AST version of the pin, for the paths the payloads go through.

        Behavioural gates can be satisfied by a renderer that happens to
        redact for another reason. This one asserts the mechanism: the
        review module's boundary and both dispatchers reference the sanitiser
        by name, so a future renderer cannot render diff content without
        breaking it.
        """
        # Each path reaches the sanitiser by its OWN mechanism, so the check
        # is per-path rather than a shared substring. `_render_review_surface`
        # calls `ui.sanitize_text` through an ATTRIBUTE, which a
        # `ast.Call`-on-`ast.Name` walk does not see - and an assertion
        # written for the wrong call shape is an assertion that cannot fire,
        # which this round's own sensitivity discipline is against.
        for module, function, wanted in (
            ("review.py", "_bound", "_sanitizer"),
            ("interactive.py", "_render_review_surface", "sanitize_text"),
            ("fileview.py", "_sanitize_diff", "sanitize_text"),
        ):
            source = (CLI / module).read_text(encoding="utf-8")
            tree = ast.parse(source)
            found = None
            for candidate in ast.walk(tree):
                if isinstance(candidate, ast.FunctionDef) and candidate.name == function:
                    found = candidate
                    break
            assert found is not None, f"{module}::{function} is gone"
            called = {
                child.func.attr
                for child in ast.walk(found)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            } | {
                child.func.id
                for child in ast.walk(found)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            assert wanted in called, (
                f"{module}::{function} no longer reaches `{wanted}`; it calls "
                f"{sorted(called)}"
            )
