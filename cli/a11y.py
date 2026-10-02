"""Terminal accessibility and long-output policy for Neo.

Three jobs, all of which the visual layer cannot do for itself:

1. **STATUS ANNOUNCEMENTS.** A status chip that is *re-rendered in place*
   is invisible to a screen reader and to a dumb terminal: both read the
   stream of new output, and an in-place update writes no new bytes. This
   module turns a state change into a **plain-text sentence** with no
   glyph, no color, and no markup, so the same information is legible
   when `TERM=dumb`, under `NO_COLOR`, and through a screen reader
   reading the terminal buffer. `AnnouncementGate` is what keeps a
   125 Hz repaint loop from turning one run into 400 announcements: it
   speaks only on a **transition**, keyed by the caller.

2. **TEXT ALTERNATIVES FOR GLYPHS.** `ui.GLYPHS` is an *encoding*
   fallback (a pretty glyph becomes ASCII when the console cannot
   encode it). That is not the same thing as a text alternative, and it
   does not help anyone: a `V` in a rail means nothing to a reader who
   cannot see the rail, and `?` for "not verified" is a shrug, not a
   word. `describe_file_state` and `describe_status` produce the words.

3. **LONG-OUTPUT PAGINATION.** The trace-detail and diff-detail bodies
   used to be `splitlines()[:400]` — a **silent** truncation. A user
   reading a 5,000-line test failure saw 400 lines and no indication
   that 4,600 were hidden, with no way to reach them. `Page` is a real
   pager: it names what it is showing, whether more exists, and it
   always has a line the caller can render so the hidden remainder is
   stated rather than implied away.

Nothing here touches the journal, the verifier, or any
`INTERFACES.md` contract. Every function is pure, total, and never
raises: a presentation helper that can crash the shell it is
describing is worse than one that says less.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Longest announcement sentence. A screen reader reads a whole line in
#: one breath; an announcement longer than this stops being an
#: announcement and becomes the wall the caller was avoiding.
ANNOUNCEMENT_MAX_CHARS = 240

#: Default page size when a caller does not pick one. Chosen to leave
#: room for a title, a hint, and a page label inside an 80x24 terminal.
DEFAULT_PAGE_SIZE = 24

#: Shown after a bounded view when the caller is NOT paginating, so a
#: bound is never silent.
OMITTED_MARKER = (
    "... {count} more line(s) not shown; open the detail view for the rest."
)


# ---------------------------------------------------------------------------
# 1. Plain-text status announcements
# ---------------------------------------------------------------------------


def _words(value: Any, limit: int = 60) -> str:
    """Return sanitized single-line text, or "" for nothing usable."""
    from cli import ui as _ui

    text = _ui.strip_ansi(value if isinstance(value, str) else str(value or ""))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(1, limit - 3)] + "..."
    return text


def _clip(text: str) -> str:
    """Bound an announcement sentence without cutting mid-word silently."""
    if len(text) <= ANNOUNCEMENT_MAX_CHARS:
        return text
    return text[: ANNOUNCEMENT_MAX_CHARS - 3].rstrip() + "..."


def announce_idle() -> str:
    """Announce that nothing is running. Plain text; no glyph."""
    return "Status: idle. No task is running."


def announce_run_started(task_id: Any, mode: Any = "") -> str:
    """Announce that a run began, naming the task and the mode.

    The task id is included because it is the one handle a user needs in
    order to cancel, watch, or resume. It is named here rather than only
    in the header because the header is an in-place region.
    """
    parts = ["Task started"]
    task = _words(task_id, 40)
    if task:
        parts.append(f"id {task}")
    mode_text = _words(mode, 32)
    if mode_text:
        parts.append(f"mode {mode_text}")
    return _clip(". ".join(parts) + ".")


def announce_queued() -> str:
    """Announce that the run has begun but has no journal task id yet."""
    return "Task started. Waiting for the task journal; no task id yet."


def announce_phase(phase: Any, *, events: Optional[int] = None) -> str:
    """Announce the current phase of a live run.

    `phase` is whatever the journal projected. It is cleaned to one line
    of plain words: an announcement carrying markup or a color escape is
    read as punctuation by a screen reader and as mojibake on a dumb
    terminal, which defeats the entire purpose of writing one.
    """
    text = _words(phase, 90) or "working"
    tail = f" {events} events so far." if events else ""
    return _clip(f"Working: {text}.{tail}")


def announce_tool_pending(
    tool: Any, command: Any = "", elapsed_s: Optional[int] = None
) -> str:
    """Announce a slow tool, naming the tool and the elapsed seconds.

    The distinction this exists for: "the provider is slow" and "the
    process is wedged" look identical until you see elapsed time. A user
    who cannot see the spinner still gets the number.
    """
    name = _words(tool, 24) or "tool"
    body = f"Tool running: {name}."
    detail = _words(command, 60)
    if detail:
        body += f" Command: {detail}."
    if elapsed_s is not None:
        body += f" {int(elapsed_s)} seconds so far."
    return _clip(body)


def split_action(label: Any) -> Tuple[str, str]:
    """Split the run line's `action · command` pending label.

    The pending label is built as one readable string
    (`"running a command · pytest -q"`), and speaking it verbatim is
    better than re-deriving a tool name from it. This returns
    ``(action, command)`` so a caller can still attach the elapsed
    seconds to the right half without the tool name being repeated
    twice. An unlabelled string returns ``(label, "")``.
    """
    text = _words(label, 160)
    if not text:
        return ("", "")
    if "·" in text:
        action, _, command = text.partition("·")
        return (action.strip(), command.strip())
    return (text, "")


def announce_approval(kind: Any, subject: Any = "") -> str:
    """Announce that the run is waiting on a human decision.

    `kind` and `subject` are the approval request's own effect and
    target. Naming them is the point: a bare "waiting for approval" tells
    a user nothing about what is being asked of them.
    """
    effect = _words(kind, 60) or "an action"
    body = f"Approval needed: {effect}."
    target = _words(subject, 60)
    if target:
        body += f" Target: {target}."
    body += " Press y to allow once, n to reject, esc to cancel."
    return _clip(body)


def announce_cancel_requested() -> str:
    """Announce that a cancel was asked for and is not yet complete."""
    return (
        "Cancel requested. Stopping at the next safe point; "
        "the run is not finished until the journal says so."
    )


def announce_steered(intent: Any = "") -> str:
    """Announce that steering text was delivered to a live run."""
    text = _words(intent, 40)
    body = "Steering delivered to the running task."
    if text:
        body += f" Intent: {text}."
    return _clip(body)


def announce_finished(
    status: Any,
    *,
    verified: bool = False,
    files: Optional[Sequence[Any]] = None,
    elapsed_s: Optional[Any] = None,
    cost: Any = "",
    calls: Optional[int] = None,
) -> str:
    """Announce a finished run in words, and never upgrade its verdict.

    This is the one announcement with a truthfulness requirement, so the
    rule is mechanical: **the word "verified" appears only when the
    caller passed `verified=True`.** A bare `"success"`, `"done"`, or
    `"ok"` status with no clean evidence reads as
    `"finished, not verified"` — because dressing an unverified run as
    verified is the exact defect this repository keeps having to fix in
    one surface at a time, and an announcement is just another surface.

    The failure branch is stated in words too (`"failed"`), so the
    announcement never depends on a red chip to communicate it.
    """
    raw = _words(status, 40).lower() or "unknown"
    failed = raw in {"error", "failed", "timeout", "crash", "cancelled", "canceled"}
    if failed:
        # "Task failed", not "Task finished: error". A screen reader reads
        # the sentence, and "finished" is a word a person hears as success
        # before reaching the second half.
        body = f"Task failed: {raw}."
        if not verified:
            body += " Not verified."
    elif verified:
        body = "Task finished: verified by the test suite."
    else:
        body = f"Task finished: {raw}, not verified."
    changed = list(files or [])
    if changed:
        noun = "file" if len(changed) == 1 else "files"
        body += f" {len(changed)} {noun} changed."
    if calls:
        body += f" {int(calls)} model call(s)."
    if elapsed_s not in (None, ""):
        try:
            body += f" {int(float(elapsed_s))} seconds."
        except (TypeError, ValueError):
            pass
    cost_text = _words(cost, 24)
    if cost_text:
        body += f" Cost {cost_text}."
    return _clip(body)


def announce_failure(reason: Any, *, next_steps: Sequence[Any] = ()) -> str:
    """Announce a failure with at least one runnable next action.

    A failure sentence that names no next step is a dead end for a user
    who cannot see the card, so `next_steps` is appended when present and
    `/doctor` + `/trace` are the documented fallback callers can supply.
    """
    detail = _words(reason, 100) or "unknown error"
    body = f"Task failed: {detail}."
    steps = [_words(step, 40) for step in (next_steps or []) if _words(step, 40)]
    if steps:
        body += " Next: " + "; ".join(steps[:3]) + "."
    return _clip(body)


def announce_paged(label: Any, hint: Any = "") -> str:
    """Announce a navigation step so a page turn is not silent."""
    body = f"View: {_words(label, 60) or 'updated'}."
    extra = _words(hint, 60)
    if extra:
        body += f" {extra}"
    return _clip(body)


class AnnouncementGate:
    """Suppresses CONSECUTIVE repeats; a returning transition speaks again.

    The repaint loop runs at 125 Hz and the phase string repeats, so
    without a gate every announcement would be noise. But the gate keys
    on the **previous** transition, not on a set of every key ever seen,
    and the difference matters:

    - Consecutive duplicates are suppressed. A user hears "Working:
      editing." once, not once per eighth of a second.
    - A phase that comes BACK after something else speaks again. The
      run line says `model: thinking (step-1)` for every step, and a
      screen reader that heard it only the first time would miss steps
      2 through 9 entirely. The user was looking at the screen in
      between, so the transition is new information.

    It also stores exactly one transition, so the memory is O(1) for the
    life of a session rather than one entry per distinct phase string a
    long run can produce.
    """

    def __init__(self) -> None:
        self._last_key: Any = None
        self._last_sentence: str = ""
        self.last: str = ""

    def offer(self, key: Any, text: Any) -> Optional[str]:
        """Return `text` when it is not a consecutive repeat, else ``None``.

        An empty or unusable `text` is never offered — announcing nothing
        is worse than announcing nothing, because it would consume the
        key and suppress the real announcement that follows.
        """
        sentence = _words(text, ANNOUNCEMENT_MAX_CHARS)
        if not sentence:
            return None
        if key == self._last_key and sentence == self._last_sentence:
            return None
        self._last_key = key
        self._last_sentence = sentence
        self.last = sentence
        return sentence

    def reset(self) -> None:
        """Forget the last transition. Used when a new run starts."""
        self._last_key = None
        self._last_sentence = ""
        self.last = ""


# ---------------------------------------------------------------------------
# 2. Text alternatives for glyphs
# ---------------------------------------------------------------------------

#: Glyph -> the words a reader needs instead. Keys are the glyph
#: CHARACTERS, not the `ui.GLYPHS` names, so this table also covers the
#: ASCII fallbacks a legacy console gets. A glyph absent from the table
#: renders as itself, which is the honest "no alternative declared"
#: answer rather than a guess.
GLYPH_TEXT = {
    "\u2714": "ok",
    "\u2718": "failed",
    "\u2716": "failed",
    "\u25cf": "bullet",
    "\u2022": "bullet",
    "\u25c6": "marker",
    "\u2192": "arrow",
    "\u203a": "prompt",
    "\u23f1": "timer",
    "\u25cb": "pending",
    "\u25b8": "active",
    "\u2713": "ok",
    "*": "marker",
    ">": "prompt",
    "OK": "ok",
    "x": "failed",
    "T": "timer",
    "-": "bullet",
}

#: The compact file-state codes the rails render, and what each letter
#: MEANS. The rail stays compact for density; this table is what an
#: announcement, a detail view, or a reader expands it with, so the
#: letters are never the only place the meaning lives.
FILE_STATE_LEGEND = {
    "S": "staged",
    "U": "unstaged",
    "V": "verified",
    "?": "not verified",
    "cp": "in a checkpoint",
}

#: Separator between a code and its expanded words, chosen because it
#: cannot be confused with a path character in a terminal.
_STATE_SEPARATOR = "="


def glyph_text(glyph: Any) -> str:
    """Return the text alternative for one glyph, or "" when undeclared.

    An undeclared glyph returns "" rather than a guess: inventing a word
    for a mark nobody defined is how a wrong label becomes a permanent
    fact in a help screen.
    """
    return GLYPH_TEXT.get(str(glyph or ""), "")


def file_state_codes(
    *, staged: bool = False, verified: bool = False, checkpoint: bool = False
) -> str:
    """Return the compact rail code for one file's state.

    `?` is the "not verified" mark. It is a mark, NOT a word, which is
    exactly why `describe_file_state` exists — call anything rendered
    where a reader may see it through `describe_file_state` instead.
    """
    parts = ["S" if staged else "U", "V" if verified else "?"]
    if checkpoint:
        parts.append("cp")
    return "".join(parts)


def describe_file_state(
    path: Any = "",
    *,
    staged: bool = False,
    verified: bool = False,
    checkpoint: bool = False,
) -> str:
    """Return one file's state as a WORDED line: the glyph alternative.

    Example: ``app.py unstaged, not verified, in a checkpoint``. The path
    is first because that is what the user is looking for; the states
    follow in the same order the rail code uses, so the compact form and
    the expanded form are read in the same sequence.
    """
    words = [
        FILE_STATE_LEGEND["S"] if staged else FILE_STATE_LEGEND["U"],
        FILE_STATE_LEGEND["V"] if verified else FILE_STATE_LEGEND["?"],
    ]
    if checkpoint:
        words.append(FILE_STATE_LEGEND["cp"])
    name = _words(path, 80) or "file"
    return f"{name} {', '.join(words)}."


def describe_status(text: Any, glyph: Any = "") -> str:
    """Return a status word as a sentence, with a glyph expanded.

    `glyph` is optional and only ever *appends* its alternative; the
    status word is the primary content, so a missing or unknown glyph
    still produces a complete sentence.
    """
    from cli import ui as _ui

    label = _words(_ui.strip_ansi(str(text or "")), 60) or "unknown"
    sentence = f"Status: {label}."
    alternative = glyph_text(glyph)
    if alternative and alternative not in label.lower():
        sentence = f"Status: {label} ({alternative})."
    return _clip(sentence)


def render_code(code: Any) -> str:
    """Expand one compact file-state code into its words.

    `render_code("U?")` -> ``"unstaged, not verified"``. An unknown code
    is returned as ``"code <x>"`` so a future letter can never be read as
    silence — an unrecognized mark displayed as nothing is indistinguishable
    from no state at all.
    """
    text = str(code or "").strip()
    if not text:
        return ""
    words: List[str] = []
    rest = text
    while rest:
        if rest[:2] in FILE_STATE_LEGEND:
            words.append(FILE_STATE_LEGEND[rest[:2]])
            rest = rest[2:]
            continue
        found = next(
            (part for part in FILE_STATE_LEGEND if rest.startswith(part)), None
        )
        if found is None:
            return f"code {text}"
        words.append(FILE_STATE_LEGEND[found])
        rest = rest[len(found) :]
    return ", ".join(words)


def legend_lines() -> List[str]:
    """Return the file-state legend as plain-text lines for /help and a11y."""
    return [f"{code} = {word}" for code, word in sorted(FILE_STATE_LEGEND.items())]


# ---------------------------------------------------------------------------
# 2b. Diagnostics — visually distinct from model output
# ---------------------------------------------------------------------------


#: Severity -> the ASCII mark. Encoding-probed glyphs live in `ui.GLYPHS`;
#: a diagnostic row uses a DIFFERENT character from the run's own
#: outcome marks on purpose, so "the model said done" and "a tool found a
#: problem" can never be mistaken for each other at a glance.
DIAGNOSTIC_SEVERITY_GLYPH = {
    "fatal": "!!",
    "error": "!!",
    "warning": "! ",
    "warn": "! ",
    "information": "i ",
    "info": "i ",
    "hint": "i ",
}

#: Provenance -> the words. A row a run recorded and a row a language server
#: is emitting RIGHT NOW are different claims with different lifetimes; a
#: panel that renders them as one list of interchangeable strings invites a
#: reader to trust a stale journal row as a live check.
DIAGNOSTIC_PROVENANCE_TEXT = {
    "journal": "journal",
    "lsp": "lsp",
}

#: Severity -> the hue token name, resolved by the caller through the theme.
#: Kept here so the panel and any other diagnostic surface agree, and so a
#: new severity cannot silently inherit a colour by omission.
DIAGNOSTIC_SEVERITY_ORDER = ("fatal", "error", "warning", "information", "hint")


def diagnostic_severity_rank(severity: Any) -> int:
    """Return a sort rank for a severity; unknown ranks LAST, not first.

    An unrecognized severity sorting ahead of `fatal` would put the least
    important row at the top of a panel someone is scanning for breakage.
    """
    text = str(severity or "").strip().lower()
    if text in DIAGNOSTIC_SEVERITY_ORDER:
        return DIAGNOSTIC_SEVERITY_ORDER.index(text)
    return len(DIAGNOSTIC_SEVERITY_ORDER)


def diagnostic_glyph(severity: Any) -> str:
    """Return the ASCII severity mark for a diagnostic row."""
    text = str(severity or "").strip().lower()
    return DIAGNOSTIC_SEVERITY_GLYPH.get(text, "??")


def diagnostic_provenance_text(value: Any) -> str:
    """Return the provenance word for a diagnostic, defaulting to `journal`.

    An unknown provenance renders as `journal` rather than as an empty cell:
    a blank provenance column is indistinguishable from "provenance was not
    recorded", which is a different claim.
    """
    text = str(value or "").strip().lower()
    return DIAGNOSTIC_PROVENANCE_TEXT.get(text, "journal")


def diagnostic_row(
    item: Mapping[str, Any],
    *,
    path: Any = "",
    line: Any = None,
    message: Any = "",
    severity: Any = "",
    provenance: Any = "",
) -> str:
    """Render one diagnostic as a plain-text row that cannot be read as prose.

    The shape is the distinction: ``<mark> <severity> <provenance>
    path:line:column - message``. Four fields the eye can pattern-match on
    before it reads a word, which is what "visually distinct from model
    output" means in a terminal — the model writes sentences into the
    transcript, and this never looks like one.

    The `path:line:column` link is emitted verbatim and never wrapped, so it
    stays a usable editor target even when the message is clipped.
    """
    severity_text = str(severity or item.get("severity") or "unknown").strip().lower()
    link_path = str(path or item.get("path") or item.get("file") or "")
    link_line = line if line is not None else item.get("line", 1)
    try:
        link_line = max(1, int(link_line))
    except (TypeError, ValueError):
        link_line = 1
    link = str(
        item.get("link") or (f"{link_path}:{link_line}" if link_path else "no file")
    )
    source = provenance or item.get("provenance")
    body = str(message or item.get("message") or "").strip()
    parts = [
        f"{diagnostic_glyph(severity_text)} {severity_text}",
        f"[{diagnostic_provenance_text(source)}]",
        link,
    ]
    if item.get("source"):
        parts.append(str(item["source"]))
    if item.get("code"):
        parts.append(str(item["code"]))
    return " ".join(part for part in parts if part) + (f" - {body}" if body else "")


def describe_diagnostic(item: Mapping[str, Any]) -> str:
    """Return one diagnostic as a full sentence for an announcement.

    Names the provenance in words, because "live" and "recorded" are the
    difference between a check that just ran and a claim a run made earlier.
    """
    link = str(item.get("link") or "")
    if not link:
        path = str(item.get("path") or item.get("file") or "")
        if path:
            try:
                line = max(1, int(item.get("line", 1)))
                column = max(1, int(item.get("column", 1)))
            except (TypeError, ValueError):
                line, column = 1, 1
            link = f"{path}:{line}:{column}"
    severity = str(item.get("severity") or "unknown").strip().lower()
    where = f" in {link}" if link else ""
    text = str(item.get("message") or "").strip() or "no message"
    return _clip(
        f"Diagnostic: {severity}{where} from "
        f"{diagnostic_provenance_text(item.get('provenance'))} - {text}"
    )


def lsp_state_sentence(report: Mapping[str, Any]) -> str:
    """Render the live-language-server receipt as one honest sentence.

    The receipt exists because the previous implementation was
    `except Exception: pass`, which made "not configured", "could not
    start", and "your code is clean" render as the same words. Each of the
    four states names itself.
    """
    state = str(report.get("state") or "not_configured")
    if state == "live":
        return _clip(
            f"Live language-server diagnostics: {int(report.get('count') or 0)} "
            "reported just now."
        )
    reason = str(report.get("reason") or "").strip()
    if state == "not_configured":
        return _clip(
            "Live language-server diagnostics: not configured for this repository."
        )
    if state == "unreadable_config":
        return _clip(
            f"Live language-server diagnostics: configuration unusable - {reason}"
        )
    return _clip(f"Live language-server diagnostics: unavailable - {reason}")


# ---------------------------------------------------------------------------
# 3. Long-output pagination
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    """An immutable window over a long body of text.

    Frozen because a pager is read by the renderer on one line and must
    not be able to mutate under it: `size` is clamped in `__post_init__`
    so a hostile or mistaken `size=0` cannot produce an empty page that
    reads as "this output is empty".
    """

    total: int
    index: int = 0
    size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", max(0, int(self.total or 0)))
        object.__setattr__(self, "size", max(1, int(self.size or 1)))
        last = self.last_index
        object.__setattr__(self, "index", max(0, min(int(self.index or 0), last)))

    @property
    def last_index(self) -> int:
        """Index of the final page; 0 when there is at most one page."""
        if self.total <= 0:
            return 0
        return max(0, (self.total - 1) // self.size)

    @property
    def pages(self) -> int:
        """Number of pages; always at least 1 so the label never divides by zero."""
        return self.last_index + 1

    @property
    def start(self) -> int:
        """First line number on this page, 1-based; 0 when there is nothing."""
        return 0 if self.total <= 0 else self.index * self.size + 1

    @property
    def end(self) -> int:
        """Last line number on this page, 1-based; 0 when there is nothing."""
        if self.total <= 0:
            return 0
        return min(self.total, (self.index + 1) * self.size)

    @property
    def hidden_before(self) -> int:
        """How many lines are on EARLIER pages."""
        return self.start - 1

    @property
    def hidden_after(self) -> int:
        """How many lines are on LATER pages. 0 on the final page."""
        return max(0, self.total - self.end)

    @property
    def has_more(self) -> bool:
        """Whether a later page exists."""
        return self.index < self.last_index

    @property
    def has_previous(self) -> bool:
        """Whether an earlier page exists."""
        return self.index > 0

    def window(self, lines: Sequence[Any]) -> List[Any]:
        """Return this page's slice of `lines`.

        Tolerates a sequence shorter or longer than `total` — it slices
        whatever it was given rather than raising, so a caller that
        recomputes the body between pages cannot crash the view.
        """
        if not lines:
            return []
        start = self.index * self.size
        return list(lines[start : start + self.size])

    def label(self) -> str:
        """Return the one line that makes truncation impossible to miss.

        Reports the exact range AND the total, and names the omitted
        remainder. A user who reads only this line still knows they are
        looking at a fragment and how much is left.
        """
        if self.total <= 0:
            return "no lines"
        if self.pages == 1:
            return f"lines 1-{self.end} of {self.total} (all of it)"
        body = f"lines {self.start}-{self.end} of {self.total} (page {self.index + 1} of {self.pages})"
        if self.hidden_after:
            body += f" · {self.hidden_after} more below"
        if self.hidden_before:
            body += f" · {self.hidden_before} above"
        return body

    def hint(self) -> str:
        """Return the pager key hint for the current state."""
        keys: List[str] = []
        if self.has_previous:
            keys.append("p previous page")
        if self.has_more:
            keys.append("n next page")
        if not keys:
            return "esc close"
        return " · ".join([*keys, "esc close"])

    def advance(self) -> "Page":
        """Return the next page, or this one at the end (never wraps)."""
        if not self.has_more:
            return self
        return Page(self.total, self.index + 1, self.size)

    def rewind(self) -> "Page":
        """Return the previous page, or this one at the start (never wraps)."""
        if not self.has_previous:
            return self
        return Page(self.total, self.index - 1, self.size)


def paginate(lines: Any, *, size: int = DEFAULT_PAGE_SIZE, index: int = 0) -> Page:
    """Build a `Page` for `lines`. Total: any input, including ``None``."""
    if lines is None:
        total = 0
    elif isinstance(lines, (str, bytes)):
        total = len(_text_of(lines).splitlines())
    else:
        try:
            total = len(list(lines))
        except TypeError:
            total = 0
    return Page(total, index, size)


def _text_of(value: Any) -> str:
    """Return text for a str/bytes body without raising."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def page_lines(text: Any, page: Page) -> List[str]:
    """Return one page of a text body as lines.

    Splitting happens here rather than at the call site so every
    long-output surface paginates on the SAME line units, which is what
    makes the `Page.total` in the label trustworthy.
    """
    lines = _text_of(text or "").splitlines()
    return page.window(lines)


def pager_body(text: Any, page: Page, *, render: Optional[Any] = None) -> List[Any]:
    """Return the renderable body for one page plus its label and hint.

    `render` maps one line to whatever the surface draws (a `rich
    `Text` for the transcript, a plain `str` for a headless dump). It
    defaults to the identity so the function is useful without one, and
    a raising `render` degrades to the plain line rather than taking the
    view down.
    """
    lines = page_lines(text, page)
    out: List[Any] = []
    for line in lines:
        if render is None:
            out.append(line)
            continue
        try:
            out.append(render(line))
        except Exception:
            out.append(line)
    out.append("")
    out.append(page.label())
    return out


def omitted_note(page: Page) -> str:
    """Return the line a NON-paginating bounded view must show.

    A bound that says nothing is how a user ends up believing a 400-line
    window was the whole story. Any surface that truncates without paging
    renders this instead of a bare slice.
    """
    hidden = page.hidden_after
    if hidden <= 0:
        return ""
    return OMITTED_MARKER.format(count=hidden)


def describe_pagination(page: Page) -> str:
    """Announce the pagination state in words (for the announce region)."""
    if page.total <= 0:
        return announce_paged("empty output")
    if page.pages == 1:
        return announce_paged(f"{page.total} lines, all shown", "esc close")
    return announce_paged(
        f"page {page.index + 1} of {page.pages}, lines {page.start} to {page.end} of {page.total}",
        page.hint(),
    )


def long_output_policy(
    lines: Any, *, page_size: int = DEFAULT_PAGE_SIZE
) -> Mapping[str, Any]:
    """Return the machine-readable receipt for one long-output decision.

    Exists so a test (or a reviewer) can assert the policy rather than
    infer it from a rendered screen: `paged` says whether the surface can
    reach the omitted lines, `total` says how many there were, and
    `visible` says how many this render actually shows.
    """
    page = paginate(lines, size=page_size)
    return {
        "total": page.total,
        "visible": min(page.size, page.total),
        "pages": page.pages,
        "page": page.index + 1,
        "hidden_after": page.hidden_after,
        "paged": page.has_more,
        "label": page.label(),
    }


# ---------------------------------------------------------------------------
# Focus order — the keyboard navigation contract
# ---------------------------------------------------------------------------

#: The order `tab` must walk in the main shell, and the ORDER of that
#: tuple is the contract — not just its membership. Documented in the
#: product (not only in a test) so a future widget added to
#: `NeoApp.compose` without a focus role is a visible omission rather
#: than an invisible one.
#:
#: Only two shell widgets are actually focusable: the transcript
#: (`RichLog.can_focus`, so a keyboard user can read and select it) and
#: the composer. The rails and the footer are read-only `Static` /
#: `Vertical` widgets, so listing them here would be a claim the product
#: cannot keep. Transcript first, composer second, and the composer is
#: the default focus on mount: a run that moved the caret out from under
#: a person mid-sentence would be worse than a tab ring that is short.
FOCUS_ORDER = ("neo-body", "neo-input")

#: Modals all follow one convention, and pinning it here is what stops
#: each new screen from inventing its own order: the FILTER/INPUT takes
#: focus on mount (so typing filters immediately, which is the VS Code
#: feel the palette already implements), and any list it filters is
#: reachable by `tab` after it. `esc` always closes.
MODAL_FOCUS_ORDER = ("modal-input", "modal-list")


def focus_order_report(
    mounted_ids: Iterable[Any], *, modal: bool = False
) -> Mapping[str, Any]:
    """Return which declared focus targets are actually mounted.

    A declared focus target that is not mounted is a **hidden control**,
    which the "no clipped controls" rule forbids. Reporting it here makes
    the check a product function rather than a test that greps a file, so
    a surface can ask the same question the test asks.

    `modal=True` checks the modal convention instead of the shell's.
    """
    present = [str(item) for item in mounted_ids if str(item or "")]
    declared = MODAL_FOCUS_ORDER if modal else FOCUS_ORDER
    ordered = [name for name in declared if name in present]
    missing = [name for name in declared if name not in present]
    return {
        "scope": "modal" if modal else "shell",
        "declared": list(declared),
        "mounted": present,
        "order": ordered,
        "missing": missing,
        "complete": not missing,
    }
