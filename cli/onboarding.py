"""cli/onboarding.py - what Neo is, what to do next, and why an empty panel is never blank.

Why this file exists
--------------------
Three different questions were being answered in three different places, each
with its own wording, and each of them answered a question a first-time user
actually asks:

1. "what IS this thing?" - `interactive._FIRST_RUN_BEATS` was three
   hand-maintained strings, printed only by the REPL. The TUI's startup state
   (`tui_components.EmptyState`) said something different, and neither one said
   what a person should TYPE.
2. "there is nothing here" - a dozen separate surfaces rendered their own short
   version of "no sessions yet" / "no diff from the last run" / "no MCP servers
   configured". A blank panel is a dead end; a short sentence with no next step
   is the same dead end in nicer clothes.
3. "how do I ask for that?" - `/help` was grouped by command, and the search was
   a fuzzy match over command summaries. A person who does not know the command
   is called `/diff` types "show me what changed", and the MEASURED result on
   this tree before this file existed was `/help` first.

So this module is the ONE owner of the three answers, as DATA:

* `AFFORDANCES` - the three controls a newcomer needs (see what changed, put it
  back, stop the run) with the reason each one exists;
* `EMPTY_STATES` - a CLOSED vocabulary of empty states, each with exactly one
  actionable sentence and one runnable next action;
* `TASKS` - the phrasings a person actually types, grouped by the task, each
  naming the command that answers it.

Every function here is pure, total, and never raises: a caller that cannot
resolve a repo, a logs root or a registry still gets a usable sentence.

Markup is never produced here
-----------------------------
Every line these functions return is PLAIN text. A first-run screen is built
from a repository name, a model name and a path - all of them DATA - and a
string that crosses into Textual's or rich's markup parser carrying an
unbalanced `[` DELETES the message instead of printing it. The two sanctioned
exits are `escape_lines` (rich's own `escape`, so it cannot drift from the
parser) and `text_lines` (returns `rich.text.Text`, which has no markup
interpretation at all - the structural answer).

The anti-clutter rule
---------------------
A group of one or two rows is not rendered, and the threshold is READ from
`cli.design` rather than restated - one rule, one number. A task group with
fewer than that many phrasings disappears from help entirely rather than
spending a heading and two lines on it. The three affordances are three entries
and therefore render; that is not luck, it is why there are exactly three.

No modal, ever
--------------
Every one of these is a sentence. A first-run modal that must be dismissed
before the product is readable is a first-run modal some people never get past,
which is the defect `cli/auth.py::first_run_hint` already retired. Nothing here
opens a screen; the mount points for a shell that wants one are in
`cli/AGENTS.md` under "Handoff to 01".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

__all__ = [
    "AFFORDANCES",
    "EMPTY_STATES",
    "Affordance",
    "FirstRun",
    "TaskRow",
    "affordance_lines",
    "anti_clutter_min_entries",
    "command_names",
    "empty_state",
    "empty_state_lines",
    "escape_lines",
    "first_run_lines",
    "next_action",
    "task_groups",
    "task_help_lines",
    "task_index",
    "task_lines",
    "task_phrasing_index",
    "task_phrasings",
    "task_search",
    "text_lines",
]


def anti_clutter_min_entries() -> int:
    """The one threshold, read from the layout authority.

    `cli/design.py` owns the anti-clutter rule (`ANTI_CLUTTER_MIN_ENTRIES`,
    currently 3) and a gate in `tests/test_design_layout.py` fails if any other
    module under `cli/` restates a layout constant. Reading it here keeps one
    number for one rule. A `cli.design` that cannot be imported falls back to
    the shipped value rather than rendering a section per single row, because
    "onboarding imports cleanly" is worth more than "onboarding imports the
    authority".
    """
    try:
        from cli import design
    except Exception:
        return 3
    try:
        return max(1, int(design.ANTI_CLUTTER_MIN_ENTRIES))
    except Exception:
        return 3


# ---------------------------------------------------------------------------
# 1. The three newcomer affordances
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Affordance:
    """One control a first-time user must be able to find without help.

    `command` is the runnable door. `key` is the keystroke, when one exists, and
    it is EMPTY in the table on purpose: the real value is read from
    `cli.commands.keyboard_shortcuts()` by `resolve_keys()`, because an
    affordance that advertises a key the product does not bind is the same
    class of lie as help promising a command that does not exist.
    """

    id: str
    command: str
    label: str
    because: str
    key: str = ""

    def describe(self) -> str:
        """One line: the command, what it does, and the key if there is one."""
        key = f"  {self.key}" if self.key else ""
        return f"{self.command}  {self.label}{key}"


#: The three controls, in the order a person needs them. Exactly three, and
#: that is load-bearing twice over: it clears the anti-clutter threshold, and
#: three is the whole answer to "what if the run goes wrong?" - read it, put it
#: back, stop it.
AFFORDANCES: Tuple[Affordance, ...] = (
    Affordance(
        id="what_changed",
        command="/diff",
        label="what changed",
        because="an agent that edits your files owes you the diff before anything else",
    ),
    Affordance(
        id="put_back",
        command="/undo",
        label="put it back",
        because="the change must be reversible by you, not by the run",
    ),
    Affordance(
        id="stop_the_run",
        command="/cancel",
        label="stop the run",
        because="a run you cannot stop is a run you cannot supervise",
    ),
)


def resolve_keys() -> Dict[str, str]:
    """Map command -> its declared shortcut label, from the live registry.

    `cli.commands.keyboard_shortcuts()` is the one table the palette, `/help`
    and the TUI's own drift gate already read, so an affordance rendered from
    it cannot teach a key that is unbound. An unavailable registry yields `{}`
    rather than raising: an onboarding sentence is worth more than an
    onboarding sentence with a key on it.
    """
    out: Dict[str, str] = {}
    try:
        from cli import commands as commands_mod

        table = commands_mod.keyboard_shortcuts() or {}
    except Exception:
        return out
    for row in table.get("commands", []) or []:
        label = str(row.get("label") or "").strip()
        name = str(row.get("command") or "").strip()
        if label and name and name not in out:
            out[name] = label
    return out


def affordance_lines(*, width: int = 78) -> List[str]:
    """The three affordances as PLAIN lines, sized to `width`.

    Wrapped on word boundaries, at most three rows, and NEVER zero. A screen
    that says "you can undo and you can stop" in prose but names neither
    command has taught nothing. Shortcut keys come from the live registry, so
    an affordance cannot advertise a keybind the product does not have. Never
    raises.
    """
    keys = resolve_keys()
    chunks = [f"{aff.command} {aff.label}" for aff in AFFORDANCES]
    key_label = ""
    for aff in AFFORDANCES:
        # The FIRST declared key only. `/cancel` declares both `ctrl+x` and
        # `ctrl+c`; naming both made the row wrap to a second line at 78
        # columns, and `/help`'s keyboard block still lists every declared key
        # (that block is the place for the complete table).
        declared = str(keys.get(aff.command, aff.key) or "")
        if declared:
            key_label = declared.split("/")[0].strip()
            break
    room = max(24, int(width))
    indent = " " * _FIELD_WIDTH
    head = "keys".ljust(_FIELD_WIDTH)
    for variant in _affordance_variants(chunks, key_label):
        wrapped = _wrap_chunks(variant, room - _FIELD_WIDTH)
        if len(wrapped) <= 3:
            return [head + wrapped[0]] + [f"{indent}{row}" for row in wrapped[1:]]
    # Every variant overflowed three rows, which only happens on a terminal
    # narrower than the shortest honest rendering. One row per affordance with
    # the command alone still names all three doors.
    return [head + _clip(chunks[0], room - _FIELD_WIDTH)] + [
        f"{indent}{_clip(row, room - _FIELD_WIDTH)}" for row in chunks[1:]
    ]


def _affordance_variants(chunks: Sequence[str], key_label: str) -> List[List[str]]:
    """Preferred renderings of the affordance row, best first.

    The key rides INSIDE the last affordance's own chunk rather than as a
    fourth item, because measured at 78 columns a separate chunk pushed the
    row onto a second line and the key is the least interesting thing on it.
    """
    if key_label:
        return [[*chunks[:-1], f"{chunks[-1]} ({key_label})"], list(chunks)]
    return [list(chunks)]


def _clip(text: str, room: int) -> str:
    """Bound one plain line to `room` columns, marking the cut.

    Only used on the narrow-terminal fallback paths. A line cut silently at
    the terminal edge reads as a complete line that happens to end oddly; the
    ellipsis is what makes the bound visible.
    """
    text = str(text)
    if len(text) <= room:
        return text
    return text[: max(1, room - 1)].rstrip() + "…"


def _wrap_chunks(chunks: Sequence[str], room: int) -> List[str]:
    """Greedy word wrap over pre-formed chunks, joined with a middle dot.

    `room` is the content width; the caller owns the indent. Never raises and
    never returns an empty list for a non-empty input.
    """
    room = max(8, int(room))
    out: List[str] = []
    current = ""
    for chunk in chunks:
        candidate = f"{current} · {chunk}" if current else chunk
        if len(candidate) <= room:
            current = candidate
            continue
        if current:
            out.append(current)
        current = chunk
    if current:
        out.append(current)
    return out


# ---------------------------------------------------------------------------
# 2. Empty states - one actionable sentence each, never a blank panel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmptyState:
    """One empty state: the fact, and the one thing that changes it.

    `sentence` is ONE sentence and it always names the thing that would fill
    the panel. `action` is runnable and is checked against the live command
    registry by the test suite, so an empty state cannot teach a command this
    build does not have. `also` is a second door, and `why` is the operator's
    context - deliberately NOT rendered by default, because a second sentence
    under the actionable one is where an empty state turns back into a wall.
    """

    id: str
    what: str
    sentence: str
    action: str
    also: str = ""
    why: str = ""

    def lines(self, *, width: int = 78) -> List[str]:
        """Render the state: the sentence, then the door(s), bounded to width.

        Both parts are bounded and the DOOR is the part that gets the fallback
        shape first: a next step cut mid-command is not a next step, so when
        the two do not fit the sentence is shortened and the door is kept
        whole.
        """
        room = max(16, int(width))
        doors = f"next: {self.action}"
        if self.also:
            doors = f"{doors}  ·  {self.also}"
        sentence = _clip(self.sentence, room)
        if len(sentence) + 2 + len(doors) <= room:
            return [f"{sentence}  {doors}"]
        return [sentence, _clip(doors, room)]

    def to_dict(self) -> Dict[str, str]:
        """The whole record, for a `--json` surface."""
        return {
            "id": self.id,
            "what": self.what,
            "sentence": self.sentence,
            "action": self.action,
            "also": self.also,
            "why": self.why,
        }


#: The CLOSED vocabulary. A state that is not here has no sentence, which is
#: how a new panel is forced to add one rather than rendering blank.
EMPTY_STATES: Mapping[str, EmptyState] = {
    state.id: state
    for state in (
        EmptyState(
            id="fresh_repo",
            what="the repository has no .neo/ setup",
            sentence="no .neo/ setup here yet \u2014 a project runs without it.",
            action="/init",
            also="neo config init-project",
            why="a project can run without it; /init writes the settings and "
            "example commands a team shares",
        ),
        EmptyState(
            id="no_sessions",
            what="no run has been recorded in this repository",
            sentence="no recorded sessions yet \u2014 every run you start is listed here.",
            action="/sessions",
            also="ask a question to start one",
            why="every run is written to the logs root and indexed here",
        ),
        EmptyState(
            id="no_sessions_match",
            what="a session filter matched nothing",
            sentence="no sessions match that filter \u2014 drop the filter to see them all.",
            action="/sessions",
            also="drop the filter to see them all",
            why="the filter grammar is status: repo: task: day: resumable",
        ),
        EmptyState(
            id="no_diff",
            what="the last run recorded no change to any file",
            sentence="no diff from the last run \u2014 nothing has changed on disk yet.",
            action="/diff",
            also="/undo once something has",
            why="a read-only run answers and edits nothing; that is the point",
        ),
        EmptyState(
            id="no_runs",
            what="this session has produced no run",
            sentence="no run in this session yet \u2014 start one by asking a question.",
            action="/status",
            also="/cost once one exists",
            why="cost, files and tests are all measured from a run's own record",
        ),
        EmptyState(
            id="no_connectors",
            what="no MCP connector is configured",
            sentence="no MCP servers/connectors configured \u2014 connectors give the agent extra tools.",
            action="/mcp",
            also="neo mcp add <label> -- <command>",
            why="a connector gives the agent extra tools; none is required",
        ),
        EmptyState(
            id="no_model",
            what="no provider or model is configured",
            sentence="no model configured yet \u2014 /connect adds a provider; questions still work.",
            action="/connect",
            also="questions, /help and /diff work without one",
            why="a run needs a model; reading the repo and searching help do not",
        ),
        EmptyState(
            id="no_permissions",
            what="no approval has ever been granted",
            sentence="no permissions granted yet \u2014 every privileged action asks you first.",
            action="/approve",
            also="/reject declines and records that",
            why="every privileged action asks first, and remembers only the "
            "scope you choose",
        ),
        EmptyState(
            id="no_plugins",
            what="no plugin is installed",
            sentence="no plugins installed \u2014 a plugin only adds skills, commands and tools.",
            action="/plugins",
            also="a plugin only adds skills, commands and tools",
            why="nothing in Neo needs a plugin to work",
        ),
    )
}

#: The seven states the brief names, as data, so the gate can assert the set is
#: covered rather than trusting that somebody remembered. Extra states are fine;
#: a MISSING one is not.
REQUIRED_EMPTY_STATES: Tuple[str, ...] = (
    "fresh_repo",
    "no_sessions",
    "no_diff",
    "no_runs",
    "no_connectors",
    "no_model",
    "no_permissions",
)

_FALLBACK_STATE = EmptyState(
    id="unknown",
    what="a surface has no declared empty state",
    sentence="Nothing to show here.",
    action="/help",
    also="/help <words> finds the command you want",
    why="an undeclared state is a gap in EMPTY_STATES, not a reason to render blank",
)


def empty_state(state_id: str) -> EmptyState:
    """The declared state for `state_id`, or the honest fallback.

    Total: an unknown id returns a state that says the id is undeclared and
    points at help, because "nothing to show" with a next step is the correct
    answer and an exception is not.
    """
    if isinstance(state_id, str):
        found = EMPTY_STATES.get(state_id.strip())
        if found is not None:
            return found
    return EmptyState(
        id=_FALLBACK_STATE.id,
        what=_FALLBACK_STATE.what,
        sentence=_FALLBACK_STATE.sentence,
        action=_FALLBACK_STATE.action,
        also=_FALLBACK_STATE.also,
        why=_FALLBACK_STATE.why,
    )


def empty_state_lines(state_id: str, *, width: int = 78) -> List[str]:
    """PLAIN lines for one empty state. Never empty, never raises."""
    try:
        return empty_state(state_id).lines(width=width)
    except Exception:
        return [_FALLBACK_STATE.sentence, f"next: {_FALLBACK_STATE.action}"]


#: The label column shared by every field on the first screen. Fixed so every
#: field lines up and so a wrapped line's continuation indent is ONE number
#: rather than a per-caller decision. Eight, because the longest label is
#: "welcome" and a label that touches its own body reads as a typo.
_FIELD_WIDTH = 8


# ---------------------------------------------------------------------------
# 3. Help by TASK, not by command name
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskRow:
    """One phrasing a person types, and the command that answers it.

    `group` is the TASK ("see what changed"), not the command. `phrase` is what
    the person would say. `words` are the extra vocabulary for the SAME
    answer - "kill it", "it's wedged", "abort" - which is why
    "stop the run" and "kill it" reach the same row.

    The grouping is the point: a user does not know the command is called
    `/diff`, so an index whose only labels are command names answers a question
    nobody asked.
    """

    group: str
    phrase: str
    command: str
    words: Tuple[str, ...] = ()
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group": self.group,
            "phrase": self.phrase,
            "command": self.command,
            "words": list(self.words),
            "summary": self.summary,
        }


def _row(
    group: str,
    phrase: str,
    command: str,
    words: str,
    summary: str = "",
) -> TaskRow:
    return TaskRow(
        group=group,
        phrase=phrase,
        command=command,
        words=tuple(w for w in words.split() if w),
        summary=summary,
    )


#: The task catalogue. Eight groups; every group clears the anti-clutter
#: threshold so a heading is never spent on one or two rows.
TASKS: Tuple[TaskRow, ...] = (
    _row(
        "see what changed",
        "show me what changed",
        "/diff",
        "changed changes change modified edited touched files patch diff "
        "review inspect touched wrote wrote",
        "every file the run touched, with the diff",
    ),
    _row(
        "see what changed",
        "what did you change",
        "/diff",
        "changed changes change touch touched wrote edit edited files patch",
        "the diff, per file",
    ),
    _row(
        "see what changed",
        "what happened to my code",
        "/diff",
        "happened code files touched changed what to my",
        "the diff, per file",
    ),
    _row(
        "see what changed",
        "which files did you touch",
        "/files",
        "files touched changed list tree scope repo changed",
        "the changed-file roster",
    ),
    _row(
        "see what changed",
        "show me what it did",
        "/review",
        "review rationale summary explain did what",
        "the diff plus why the run made it",
    ),
    _row(
        "put it back",
        "undo that",
        "/undo",
        "undo revert restore roll back rollback put back reverse mistake "
        "oops regret wrong",
        "stage a revert of the last turn",
    ),
    _row(
        "put it back",
        "put that back",
        "/undo",
        "undo revert restore back original before again",
        "stage a revert of the last turn",
    ),
    _row(
        "put it back",
        "revert the change",
        "/undo",
        "undo revert rollback restore back discard",
        "stage a revert of the last turn",
    ),
    _row(
        "put it back",
        "get my files back",
        "/undo",
        "undo revert restore files back original",
        "put a file back the way it was",
    ),
    _row(
        "stop the run",
        "stop the run",
        "/cancel",
        "stop cancel kill abort halt quit interrupt wedged stuck hang kill it enough",
        "cancel safely, keeping checkpoints",
    ),
    _row(
        "stop the run",
        "it's stuck",
        "/cancel",
        "stuck wedged hung hanging hang frozen slow kill stop abort",
        "cancel safely, keeping checkpoints",
    ),
    _row(
        "stop the run",
        "kill it",
        "/cancel",
        "kill stop abort cancel halt end",
        "cancel safely, keeping checkpoints",
    ),
    _row(
        "stop the run",
        "start over",
        "/cancel",
        "over restart again from scratch begin reset",
        "cancel, then ask again",
    ),
    _row(
        "is it actually right",
        "did the tests pass",
        "/status",
        "tests passed verify verified checked proof status correct right sure evidence",
        "the run's own verification state",
    ),
    _row(
        "is it actually right",
        "what is it doing right now",
        "/status",
        "status progress doing now running where state phase",
        "what the run is doing",
    ),
    _row(
        "is it actually right",
        "show me the evidence",
        "/trace",
        "evidence trace proof logs detail record journal verify",
        "the run's own event record",
    ),
    _row(
        "redirect it",
        "actually do this instead",
        "/steer",
        "instead actually no wait rather redirect steer change mind different",
        "change the plan mid-run",
    ),
    _row(
        "redirect it",
        "no, do it this way",
        "/steer",
        "instead no way rather steer change mind correction",
        "change the plan mid-run",
    ),
    _row(
        "redirect it",
        "make the plan better",
        "/plan",
        "plan replan steps approach strategy better",
        "the plan a run would follow",
    ),
    _row(
        "what did that cost",
        "how much did that cost",
        "/cost",
        "cost money spend spendings price billing budget tokens tokens "
        "dollars receipts ledger",
        "calls, tokens and spend",
    ),
    _row(
        "what did that cost",
        "how many tokens",
        "/cost",
        "tokens cost spend usage context window",
        "calls, tokens and spend",
    ),
    _row(
        "what did that cost",
        "which model was that",
        "/model",
        "model which what provider tier",
        "the model in force",
    ),
    _row(
        "find an earlier conversation",
        "what did I do yesterday",
        "/sessions",
        "yesterday history past previous earlier sessions day runs before",
        "every recorded run",
    ),
    _row(
        "find an earlier conversation",
        "continue where I left off",
        "/resume",
        "continue resume pick up left off interrupted crashed stopped where",
        "continue a recorded run",
    ),
    _row(
        "find an earlier conversation",
        "show me my old chats",
        "/history",
        "old chats history past input earlier typed",
        "what you have typed before",
    ),
    _row(
        "get it running",
        "add a provider",
        "/connect",
        "provider connect key api login auth account credential token",
        "store a provider key, saving first",
    ),
    _row(
        "get it running",
        "which model should I use",
        "/model",
        "model which should choose pick default",
        "the model in force",
    ),
    _row(
        "get it running",
        "something is broken",
        "/doctor",
        "broken health doctor check diagnose wrong failing setup why stuck fail",
        "machine-readable health checks",
    ),
    _row(
        "get it running",
        "what can I say",
        "/help",
        "help commands usage what can how do i",
        "every command, searchable",
    ),
    _row(
        "get it running",
        "connect my tools",
        "/mcp",
        "connectors mcp tools servers external",
        "the connector registry",
    ),
)

_TASK_BY_COMMAND: Dict[str, List[TaskRow]] = {}
for _r in TASKS:
    _TASK_BY_COMMAND.setdefault(_r.command, []).append(_r)


def task_index() -> List[TaskRow]:
    """Every task phrasing, in catalogue order. A copy, so a caller cannot
    reorder the module's table by sorting what it was handed."""
    return list(TASKS)


def command_names() -> Tuple[str, ...]:
    """Every command the live registry declares, sorted.

    Used to keep the catalogue honest: a phrasing that names a command this
    build does not have is a row that teaches a door with no room behind it.
    An unavailable registry yields `()` and the gate then defers to the
    catalogue rather than failing the product.
    """
    try:
        from cli import commands as commands_mod

        return tuple(
            sorted(
                str(getattr(spec, "name", "") or "")
                for spec in commands_mod.COMMAND_SPECS
                if getattr(spec, "name", "")
            )
        )
    except Exception:
        return ()


def task_groups() -> List[Tuple[str, List[TaskRow]]]:
    """`(group, rows)` in catalogue order, anti-clutter filtered.

    A group with fewer entries than the authority's threshold is DROPPED, not
    rendered with a short list: a heading plus two rows costs three lines to
    say almost nothing. Groups keep the order they were declared in, which is
    the order a person needs them (what changed -> put it back -> stop it).
    """
    threshold = anti_clutter_min_entries()
    out: List[Tuple[str, List[TaskRow]]] = []
    seen: Dict[str, int] = {}
    for row in TASKS:
        seen[row.group] = seen.get(row.group, 0) + 1
    for group in dict.fromkeys(row.group for row in TASKS):
        rows = [row for row in TASKS if row.group == group]
        if len(rows) >= threshold:
            out.append((group, rows))
    return out


def task_search(query: str, *, limit: int = 8) -> List[TaskRow]:
    """Rank task phrasings against a free-text QUERY.

    This is the primitive the brief's fifth requirement needs: a person types
    a task, not a command name. It delegates the scoring to
    `cli.fuzzy.phrase_score`, which scores the query as a WHOLE PHRASE against
    a whole phrase - the reason `fuzzy_score`, designed to AND individual
    words against one long candidate string, ranked `/help` above `/diff` for
    "show me what changed".

    An empty query returns the whole catalogue (score 0 for every row, original
    order preserved) so a caller can render the index without a second code
    path. A query matching nothing returns `[]`; the caller says so honestly
    rather than falling back to the whole wall.
    """
    needle = str(query or "").strip()
    if not needle:
        return task_index()[: max(1, int(limit or 1))]
    try:
        from cli import fuzzy
    except Exception:
        return []
    scored: List[Tuple[int, int, TaskRow]] = []
    for index, row in enumerate(TASKS):
        # The PHRASE is the haystack, and the synonym bag is a fallback scored
        # strictly below it. Measured: without that split, "show me the
        # evidence" tied at 40010 between the row whose phrase IS that
        # sentence (/trace) and the row that merely lists the word "evidence"
        # (/status), and the tie was broken by catalogue order - which put the
        # wrong command first for a question a person really asks.
        score = fuzzy.phrase_score(needle, row.phrase)
        if score is None:
            score = fuzzy.phrase_score(needle, " ".join(row.words))
            if score is not None:
                score -= 1
        if score is None:
            continue
        # A phrasing that names the command the person typed is a fallback
        # answer, never the best one: 500 is below every phrase tier above and
        # above every coverage-tier one below.
        bare = row.command.lstrip("/")
        if bare and needle.casefold() == bare:
            score -= 500
        scored.append((-score, index, row))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored[: max(1, int(limit or 1))]]


def task_lines(*, width: int = 78) -> List[str]:
    """The bare help surface: the task index, one line per row.

    Plain text, `width` columns, grouped by TASK. Each row reads
    `<phrase>  -> <command>  <what it answers>`, so a person who does not know
    the command name finds it by describing the task, and a person who does
    know it still sees it. Never raises; an empty catalogue renders nothing
    rather than a heading over nothing.
    """
    rows: List[str] = []
    room = max(24, int(width))
    for group, group_rows in task_groups():
        header = group if len(rows) == 0 else ""
        if header:
            rows.append(header)
        else:
            rows.append("")
        for row in group_rows:
            text = f"  {row.phrase}  ->  {row.command}"
            tail = f"  {row.summary}" if row.summary else ""
            if len(text) + len(tail) <= room:
                rows.append(text + tail)
            elif len(text) <= room:
                rows.append(text)
            else:
                rows.append(text[: max(1, room - 1)].rstrip() + "…")
    return rows


def task_help_lines(query: str, *, width: int = 78) -> List[str]:
    """The QUERY form of the task index: the ranked tasks, then a nudge.

    A query that matches nothing returns `[]`, and the caller renders its own
    honest sentence. A render helper that answered "nothing here" for a typo
    would be indistinguishable from a broken search.
    """
    matches = task_search(query)
    if not matches:
        return []
    room = max(24, int(width))
    rows = [f"tasks matching {query!r}:"]
    for row in matches:
        text = f"  {row.phrase}  ->  {row.command}"
        tail = f"  {row.summary}" if row.summary else ""
        rows.append(
            text + tail if len(text) + len(tail) <= room else text[: max(1, room - 1)]
        )
    return rows


# ---------------------------------------------------------------------------
# 4. The genuine first run: what it is, one worked example, one next action
# ---------------------------------------------------------------------------

#: ONE sentence, because the first thing a person reads decides whether they
#: read the second thing. "verified, not vibed" is the promise, and the
#: promise is a claim about VERIFICATION rather than about prose - so it names
#: the check rather than the vibe.
WHAT_NEO_IS = (
    "Neo takes a plain language request - a question or a change - reads the "
    "repo, and only calls a run verified when the tests actually pass."
)

#: ONE worked example, in the two shapes a person will actually use. Both are
#: real inputs to this product, not illustrations of it, and the difference
#: between them - whether anything changes on disk - is the thing a newcomer
#: cannot guess.
WORKED_EXAMPLES: Tuple[Tuple[str, str], ...] = (
    (
        "ask",
        '"why does mean() return the sum?" reads and answers, changes nothing',
    ),
    (
        "ask",
        '"mean() returns the sum, fix it" plans, edits, tests, shows the diff',
    ),
)

#: The ONE next action. Two branches and no third: either there is a provider
#: or there is not, and "add one" / "type the example" is the whole answer.
NEXT_ACTION_WITH_PROVIDER = (
    "type one of the two above; /help <words> finds any command by its task"
)
NEXT_ACTION_WITHOUT_PROVIDER = (
    "/connect adds a provider so a run can happen (/help works without one)"
)

#: Bounded so a first-run screen cannot become the wall `/help` used to be, and
#: enforced by DROPPING the one optional field (`facts`) rather than by slicing
#: lines: a first screen truncated mid-sentence teaches a sentence that is not
#: true, and a first screen that lost its `keys` row lost the three affordances
#: the brief requires on the first screen. MEASURED: the cap holds at every
#: width >= 78 (swept 40..204, `tests/test_onboarding_surface.py` re-runs the
#: sweep). Below 78 the six required fields win and the screen gets taller -
#: 12 lines at 72, 19 at 40 - with zero overflowing lines and all six fields
#: still present. That is the honest trade and it is stated in `cli/AGENTS.md`
#: rather than hidden.
MAX_FIRST_RUN_LINES = 8
MIN_FIRST_RUN_WIDTH = 78

#: The label column. See `_FIELD_WIDTH` above; it is one number, not two.


@dataclass(frozen=True)
class FirstRun:
    """Everything the first screen needs, as values.

    Deliberately holds FACTS (a repository name, a model, a logs root, whether
    a provider is connected) and not widgets: a first-run screen that has to
    decide what to draw before it can say anything is the modal this product
    retired. `lines()` is pure, so the REPL, the TUI and a test all get the
    same bytes.
    """

    repo: str = ""
    model: str = ""
    log_root: str = ""
    connected: bool = False
    width: int = 78

    # -- the three required beats ------------------------------------------
    def what(self) -> str:
        """What Neo is. One sentence, and it is the promise."""
        return WHAT_NEO_IS

    def examples(self) -> Tuple[Tuple[str, str], ...]:
        """One worked example, in both shapes a person will use."""
        return WORKED_EXAMPLES

    def next_action(self) -> str:
        """The ONE next action, chosen by whether a provider exists."""
        return (
            NEXT_ACTION_WITH_PROVIDER
            if self.connected
            else NEXT_ACTION_WITHOUT_PROVIDER
        )

    # -- facts --------------------------------------------------------------
    def identity(self) -> List[Tuple[str, str]]:
        """`label -> value` for WHAT THIS SESSION IS: the repo and the model.

        On the `welcome` row rather than the `facts` row, because identity is
        the thing a person needs before they read anything else and a second
        line spent on it pushes the honest facts off the screen.
        """
        rows: List[Tuple[str, str]] = []
        if self.repo:
            rows.append(("repo", self.repo))
        if self.model:
            rows.append(("model", self.model))
        return rows

    def facts(self) -> List[Tuple[str, str]]:
        """`label -> value` for HOW THE SESSION IS WIRED: provider and logs.

        An unset fact is OMITTED, never rendered as a zero or a dash: a row
        that says `model -` reads as a failed lookup rather than as "unset".
        `provider` is always present because "none yet" is itself an
        actionable fact.
        """
        rows: List[Tuple[str, str]] = [
            ("provider", "connected" if self.connected else "none yet")
        ]
        if self.log_root:
            rows.append(("logs", self.log_root))
        return rows

    def records(self) -> List[Tuple[str, str]]:
        """Identity and facts together, in display order. For a JSON surface."""
        return [*self.identity(), *self.facts()]

    # -- the render ---------------------------------------------------------
    def lines(self) -> List[str]:
        """The whole first screen as PLAIN lines, bounded to `width`.

        Order is deliberate: WHAT it is, then WHAT TO TYPE, then WHAT TO DO
        NEXT, then the three controls, then the facts. A first screen that
        opens with configuration teaches that configuration is the point.

        The budget is spent by dropping the one optional field, never by cutting
        a line: a first screen whose last sentence stops mid-word is worse than
        a first screen without the `facts` row, and a first screen that lost
        its `keys` row lost the three affordances the brief puts on this screen.
        """
        room = max(40, int(self.width))
        welcome = "this is Neo"
        identity = self.identity()
        if identity:
            welcome += " · " + " · ".join(f"{k} {v}" for k, v in identity)
        required: List[List[str]] = [self._field("welcome", welcome, room)]
        required.append(self._field("what", self.what(), room))
        for _label, body in self.examples():
            required.append(self._field("ask", body, room))
        required.append(self._field("next", self.next_action(), room))
        required.append(affordance_lines(width=room))
        out: List[str] = [line for section in required for line in section]
        facts = self.facts()
        if facts:
            joined = " · ".join(f"{label} {value}" for label, value in facts)
            section = self._field("facts", joined, room)
            if len(out) + len(section) <= MAX_FIRST_RUN_LINES:
                out.extend(section)
        return out

    def _field(self, label: str, text: str, room: int) -> List[str]:
        """One labelled field, wrapped under a fixed continuation indent."""
        room = max(24, int(room))
        head = str(label).ljust(_FIELD_WIDTH)
        body_room = room - _FIELD_WIDTH
        body = _wrap_words(str(text), body_room)
        if not body:
            return [head.rstrip()]
        return [head + body[0]] + [" " * _FIELD_WIDTH + row for row in body[1:]]


def _wrap_words(text: str, room: int) -> List[str]:
    """Greedy word wrap. Never drops or invents a word."""
    words = str(text).split()
    if not words:
        return []
    room = max(8, int(room))
    out: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= room:
            current = candidate
            continue
        if current:
            out.append(current)
        current = word
    if current:
        out.append(current)
    return out


def first_run_lines(
    repo: Any = None,
    log_root: Any = None,
    *,
    model: str = "",
    connected: bool = False,
    width: int = 78,
) -> List[str]:
    """The first-run screen as PLAIN lines. Pure, total, never raises.

    A caller that cannot resolve a repository or a logs root still gets the
    three required beats; the facts are omitted instead of the screen failing,
    because a first-run surface that raises is a first-run surface some people
    never see.
    """
    try:
        room = max(40, int(width or 78))
        name = Path(str(repo)).name if repo else ""
        return FirstRun(
            repo=str(name or ""),
            model=str(model or ""),
            log_root=str(log_root or ""),
            connected=bool(connected),
            width=room,
        ).lines()
    except Exception:
        room = max(40, int(width or 78))
        fallback = FirstRun(width=room)._field  # the class is the renderer
        return [
            *fallback("welcome", "this is Neo", room),
            *fallback("what", WHAT_NEO_IS, room),
            *fallback("next", NEXT_ACTION_WITHOUT_PROVIDER, room),
        ]


def next_action(*, connected: bool = False) -> str:
    """The one next action, without building the whole screen."""
    return NEXT_ACTION_WITH_PROVIDER if connected else NEXT_ACTION_WITHOUT_PROVIDER


# ---------------------------------------------------------------------------
# 5. The two markup-safe exits
# ---------------------------------------------------------------------------


def escape_lines(lines: Any) -> List[str]:
    """`lines` as rich markup, every field escaped.

    The escape is rich's OWN `escape`, not a hand-rolled bracket replacement,
    so it cannot drift from the parser it defends. A surface that only wants
    plain text should call the producer instead - but every caller in this
    tree that builds a markup string from data goes through here.
    """
    try:
        from rich.markup import escape
    except Exception:
        return [str(line) for line in (lines or [])]
    return [str(escape(str(line))) for line in (lines or [])]


def text_lines(lines: Any, style: str = "") -> List[Any]:
    """`lines` as `rich.text.Text` - no markup interpretation at all.

    The STRUCTURAL answer to injection: a `Text` is a string plus spans, so
    there is no parser between this text and the terminal and no bracket in it
    can mean anything. Preferred wherever the sink accepts `Text`.
    """
    from rich.text import Text

    return [Text(str(line), style=style or "") for line in (lines or [])]


def task_phrasings(command: str = "") -> Dict[str, Any]:
    """Command name -> the phrasings a person types to reach it.

    The menu's SEARCH corpus, and the reason a `TASKS` row is data rather than
    a rendered line: the same thirty sentences that make `/help` answer a
    question also let the `/` menu answer it, without a second vocabulary that
    could drift from the first.

    The value is a TUPLE of candidate phrasings, not one joined string, and
    that is a measured decision rather than a style. `cli.fuzzy` scores a
    phrasing with `phrase_score`, which is tiered, and joining the corpus
    destroys the tier that matters: the query "stop the run" is an EXACT match
    (100000) against the row whose phrase IS that sentence, and only 20020 - a
    scattered ordered-subsequence - against the same row once its other twelve
    phrasings have been concatenated around it. Scored per phrasing it is an
    exact match, so the tuple keeps it one.

    The bare command name is NOT in the tuple: a corpus containing its own
    name scores an exact tier against the query "diff" and outranks the row
    that is actually `/diff`, which is the failure the separate name field
    exists to prevent. The synonym bag IS included, one word per candidate
    after the phrase, so "wedged" reaches `/cancel` from a corpus that never
    says "wedged" in a sentence.

    Keyed by `command`, so a caller mapping the menu asks for the corpus of
    the row it is rendering. `command=""` returns the whole table (a copy).
    Never raises; a command the catalogue does not mention maps to `()`.
    """
    out: Dict[str, Any] = {}
    for name, rows in _TASK_BY_COMMAND.items():
        parts: List[str] = []
        for row in rows:
            if row.phrase and row.phrase not in parts:
                parts.append(row.phrase)
        for row in rows:
            for word in row.words:
                if word and word not in parts:
                    parts.append(word)
        out[str(name)] = tuple(parts)
    if not command:
        return dict(out)
    return {str(command): out.get(str(command), ())}


def task_phrasing_index() -> Dict[str, Any]:
    """Every command's phrasing corpus, copied so a caller cannot mutate it."""
    return task_phrasings("")


def onboarding_wiring() -> Dict[str, str]:
    """What calls what, for the terminal that has to mount this.

    Named rather than documented in prose because a mount point that lives
    in a chat message is a mount point that never gets wired, and one that lives
    in the module is findable with a repository search.
    """
    return {
        "first_run_lines": "print at session start; REPL calls it through "
        "interactive.render_first_run",
        "task_lines": "print under the bare /help header",
        "task_search": "merge into interactive.help_search BEFORE the "
        "command-name ranking",
        "task_phrasings": "the `/` menu's search corpus; read through "
        "cli.palette, never restated",
        "empty_state_lines": "print instead of an empty panel; REPL calls it "
        "from /sessions, /diff, /copy-diff, /mcp and /cost",
        "affordance_lines": "print on the first screen; do not put it in a modal",
        "anti_clutter_min_entries": "read from cli.design; the `/` menu's "
        "one-member-group rule reads this, never a restated number",
    }
