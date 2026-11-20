"""T1.W2.3 â€” every TODO / FIXME / HACK / placeholder in `harness/` is CLASSIFIED,
and every real gap has an inverted pin naming its owner.

`phases/DOCTRINE.md` Â§3: *"a recorded gap is one somebody can close; an
unrecorded one just gets rediscovered."* This file is the harness-side
application of that sentence. It exists because the three
`KNOWN_CLASSIFICATION_GAPS` are pinned but **nothing pins the gap CLASS itself**:
an unclassified `TODO` is invisible until somebody greps for it, and by then
they have usually forgotten which ones they already read.

## What "classified" means here

Each hit is one of three things, and the three are NOT interchangeable:

| class | meaning | what happens to it |
|---|---|---|
| `GAP` | a real hole â€” something that does not work and is not supposed to | recorded in `GAPS` with an owner and an **inverted pin** that FAILS the day it is fixed |
| `DECISION` | a deliberate, documented choice | recorded in `DECISIONS` with the reason it is correct |
| `STALE` | a comment that no longer describes the code | **deleted**, and the deletion is recorded |

A `TODO` that is really a `DECISION` is a small dishonesty â€” it tells the next
reader a thing is unfinished when it is finished. A `TODO` that is really a
`GAP` and was left unrecorded is the failure this file exists to prevent. Both
are worth a test, because both are invisible from a grep.

## Why inverted pins, not failing tests

A `GAP` gets a test that **passes today and fails when the gap is closed**. That
is backwards from every other pin in the tree, and deliberately: a gap that
makes a test fail is a broken build, so the gap gets silently "fixed" by
deleting the test, and the record goes with it. An inverted pin makes CLOSING
the gap the thing that breaks, which is the only direction in which someone
notices the record needs updating.

## Why AST and not a grep

A grep for `TODO` cannot tell a comment from a docstring from a string literal,
and this tree has all three â€” plus one real case that is none of them: a
`todo` TOOL NAME. `harness/tools.py`'s catalog has a `"todo"` entry, the kernel
has a `todo` handler, and `SessionState.todo_items` is a field. None of those is
a gap; all of them match a case-insensitive grep for `todo`. Counting them as
gaps would be a classification that means nothing.

Host-only: no Docker, no provider, no network.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

HARNESS_ROOT = Path(__file__).resolve().parent

GAP = "gap"
DECISION = "decision"
STALE = "stale"

#: The marker words, matched against COMMENTS and DOCSTRINGS only. Case-folded,
#: and `todo` is included deliberately: the three `KNOWN_CLASSIFICATION_GAPS`
#: this file coexists with are phrased that way in `harness/AGENTS.md`, so
#: excluding it would let a future gap hide behind a spelling.
MARKERS: Tuple[str, ...] = (
    "todo",
    "fixme",
    "hack",
    "xxx",
    "not implemented",
    "unimplemented",
    "for now",
    "temporarily",
    "placeholder",
    "not yet",
)

#: Every `harness/` source the audit covers, INCLUDING the `_stubs` package.
#: `_stubs` is included because a stub that reimplements a boundary by hand is
#: exactly where a `for now` comment hides a divergence, and Wave 1's redaction
#: audit found `harness/context_compiler.py` keeping a second redactor that way.
_STUB_EXCLUDED_FROM_SCAN = False


def _harness_sources() -> List[Path]:
    return sorted(
        path
        for path in HARNESS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and not path.name.startswith("test_")
    )


# ---------------------------------------------------------------------------
# The finding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerHit:
    """One marker occurrence, with enough context to classify it by reading."""

    file: str
    line: int
    marker: str
    text: str
    #: `comment` or `docstring` â€” the audit only reads these two.
    origin: str
    enclosing: str

    def as_row(self) -> str:
        return f"{self.file}:{self.line} [{self.marker}/{self.origin}] {self.text}"


def _docstring_ids(tree: ast.AST) -> Dict[int, str]:
    """`id(Constant)` -> the name of the object whose docstring it is."""
    out: Dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out[id(first.value)] = getattr(node, "name", "<module>")
    return out


def _enclosing_name(node: ast.AST, parents: Dict[int, ast.AST]) -> str:
    """The nearest enclosing function or class name for a node."""
    current = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return current.name
        current = parents.get(id(current))
    return "<module>"


def _parents(tree: ast.AST) -> Dict[int, ast.AST]:
    table: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            table[id(child)] = node
    return table


def find_marker_hits() -> List[MarkerHit]:
    """Every marker word in a `harness/` COMMENT or DOCSTRING.

    Comments come from `tokenize` (a comment is not a node in the AST), and
    docstrings from the parse tree. String literals that are neither are
    skipped â€” see `test_a_string_literal_is_not_a_marker_hit` for why that
    matters here.
    """
    import io
    import tokenize

    hits: List[MarkerHit] = []
    for path in _harness_sources():
        raw = path.read_bytes()
        relative = path.relative_to(HARNESS_ROOT).as_posix()
        source = raw.decode("utf-8-sig", errors="replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        parents = _parents(tree)
        docs = _docstring_ids(tree)
        lines = source.splitlines()

        # Docstrings, via the parse tree.
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) not in docs:
                continue
            for lineno in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                text = lines[lineno - 1] if lineno <= len(lines) else ""
                lowered = text.lower()
                for marker in MARKERS:
                    if marker in lowered:
                        hits.append(
                            MarkerHit(
                                file=relative,
                                line=lineno,
                                marker=marker,
                                text=text.strip(),
                                origin="docstring",
                                enclosing=docs[id(node)],
                            )
                        )
                        break

        # Comments, via tokenize.
        try:
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type != tokenize.COMMENT:
                    continue
                lowered = token.string.lower()
                for marker in MARKERS:
                    if marker in lowered:
                        hits.append(
                            MarkerHit(
                                file=relative,
                                line=token.start[0],
                                marker=marker,
                                text=token.string.strip(),
                                origin="comment",
                                enclosing=_enclosing_name(tree, parents)
                                if False
                                else _enclosing_at(parents, token.start[0]),
                            )
                        )
                        break
        except tokenize.TokenError:  # pragma: no cover - unterminated string
            continue

    return sorted(hits, key=lambda h: (h.file, h.line, h.marker))


def _enclosing_at(parents: Dict[int, ast.AST], lineno: int) -> str:
    """The enclosing function/class of a line, best effort."""
    best = "<module>"
    best_start = -1
    for _node_id, node in parents.items():
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start)
        if start <= lineno <= end and start > best_start:
            best_start = start
            best = node.name
    return best


HITS = find_marker_hits()


# ---------------------------------------------------------------------------
# The three classifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedGap:
    """A real hole: something that does not work and is not supposed to."""

    file: str
    line: int
    marker: str
    what_is_missing: str
    owner: str
    #: The INVERTED pin: a test that passes today and FAILS when the gap closes.
    inverted_pin: str
    #: Why this must not be closed in this wave.
    why_not_here: str


@dataclass(frozen=True)
class RecordedDecision:
    """A deliberate choice that happens to contain a marker word."""

    file: str
    line: int
    marker: str
    why_correct: str
    #: P3a: an optional STABLE key. `line` is fragile - P1/W2-T1 recorded that
    #: "a line-keyed classification is silently invalidated by any insertion into
    #: a heavily-pinned shared file", and P3a then proved it by inserting into
    #: `agent_kernel/conversation.py` twice and having the existing
    #: `conversation.py` record go stale. A record that names a SUBSTRING of its
    #: own hit survives every future insertion, so a record MAY carry one and
    #: `_classified()` prefers it over the line. `line` is still required so a
    #: reader can navigate to the hit, not because the classifier depends on it.
    stable_text: str = ""


#: Every REAL gap found by the audit, with an owner and an inverted pin.
#:
#: **One real gap, and it was NOT found by grepping for `TODO`.**
#: The audit found ten marker hits in `harness/` (see `HITS`), read every one,
#: and classified all ten as DECISIONS. Then it ran `pytest harness/` — which is
#: what an audit of this directory should do — and found a defect no marker-word
#: scan would ever surface: `harness/test_config.py` is a production module named
#: `test_*.py`, so pytest collects it and then fails to collect its own public
#: API as a test. That is recorded in `GAPS` below with an inverted pin.
#:
#: **The marker-word classification is a complete answer to the wrong question.**
#: The brief asked for every TODO/FIXME/HACK/placeholder to be classified, and
#: all ten are. The brief's underlying concern was `phases/DOCTRINE.md` §3 — "an
#: unrecorded gap just gets rediscovered" — and the gap that actually exists in
#: `harness/` this round is not a marker word at all.
#:
#: The two marker-word calls nearest to being gaps are recorded in `DECISIONS`
#: with their reasoning spelled out, because "none of them is a gap" is a claim
#: a reader is entitled to check:
#:
#: * `harness/agent_kernel/conversation.py:1065` â€” "the handoff is not yet
#:   counted against the protected region". This states a real accounting gap
#:   in a comment, with no pin. It is classified as a DECISION because the
#:   comment is TRUE and `harness/AGENTS.md` records the follow-up â€” but if
#:   that follow-up is ever dropped, this is the comment that will have
#:   outlived it, so it is the first thing to re-read.
#: * `harness/trace.py:41,43` â€” the `LEGACY_REDACTED` placeholder's own
#:   comments. A compatibility constant is exactly the kind of thing a future
#:   cleanup deletes without reading the consumer it exists for.
#:
#: The table exists anyway, with a non-empty assertion below, because an EMPTY
#: gap table that nothing pins is indistinguishable from a gap table nobody
#: filled in.
GAPS: Tuple[RecordedGap, ...] = (
    RecordedGap(
        file="harness/test_config.py",
        line=1205,
        marker="(no marker word - found by a collection error)",
        what_is_missing=(
            "`harness/test_config.py` is a PRODUCTION module (R2-03's "
            "test-configuration guard), not a test file, and it is named "
            "`test_*.py` — so pytest collects it, and collects its public "
            "`test_config_guard(pristine_dir, work_dir)` FUNCTION as a test with "
            "two fixtures that do not exist. Every `pytest harness/` run therefore "
            "ends in `ERROR harness/test_config.py::test_config_guard: fixture "
            "'pristine_dir' not found`.\n"
            "This is a real defect and it is NOT a marker-word gap, so it was "
            "found by running the audit's own directory rather than by grepping "
            "for `TODO`. It is recorded here because the round's scope forbids "
            "implementing a recorded gap: the fix is a rename of a public "
            "function that `harness/editor.py:647` imports and that "
            "`tests/test_ceiling_r2_03_config_guard.py` calls at seven sites, "
            "which is a shared decision, not a side effect of an audit."
        ),
        owner=(
            "T1 (harness/test_config.py) + T5 (tests/test_ceiling_r2_03_config_"
            "guard.py). One of: rename the module to `config_guard.py` (cleanest "
            "- nothing outside harness/ imports the module name), or rename the "
            "function to `evaluate_test_config_guard`. Either needs the "
            "seven call sites in tests/ updated in the same change."
        ),
        inverted_pin="test_the_test_config_module_collection_error_is_still_there",
        why_not_here=(
            "Renaming a public function across harness/ + tests/ in a pin-only "
            "wave expands scope past what the round was asked to do, and a "
            "half-renamed public function breaks the editor's edit gate."
        ),
    ),
)


#: Every marker hit that is a DELIBERATE choice, with the reason it is correct.
#:
#: These are the ones a grep would report as unfinished work and that are not.
#: Each entry says why the choice is right, because "it is fine" is not a reason
#: a future reader can check.
DECISIONS: Tuple[RecordedDecision, ...] = (
    # ---------------------------------------------------------------------
    # P3a. Every one of these is the word "placeholder" used as a DOMAIN TERM
    # for the text that replaces a cleared tool result, plus one "not yet" that
    # is an ordinary English adverb. All thirteen were read individually.
    #
    # They are recorded as ONE class with a shared reason because a corpus whose
    # every row has its own bespoke justification invites the next writer to
    # invent one, and because the alternative - rewording thirteen accurate
    # comments to satisfy a word list - would make the code worse to read for no
    # gain. `stable_text` is what distinguishes the rows.
    # ---------------------------------------------------------------------
    RecordedDecision(
        file="agent_kernel/clearing.py",
        line=10,
        marker="placeholder",
        stable_text="clearing      old ``tool_result`` -> placeholder LOSSLESS",
        why_correct=(
            "The module docstring's THREE-LEVER TABLE. 'placeholder' names the "
            "artefact - the short text that stands in for a cleared tool result - "
            "and the row exists to contrast clearing against compaction. It is a "
            "domain term here, not a stub for missing code, and the contrast is "
            "the module's whole argument: compaction is lossy and this is not."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/clearing.py",
        line=132,
        marker="placeholder",
        stable_text="a clearing costs more in placeholder text than it",
        why_correct=(
            "The comment on `DEFAULT_MIN_CHARS_TO_CLEAR`. It states a MEASURED "
            "reason for the 160-character floor: below it, the placeholder costs "
            "more context than the result it replaces, so clearing would be a net "
            "loss. The bound is implemented and pinned "
            "(`test_a_run_of_too_small_results_is_not_cleared` is covered by "
            "`test_the_receipt_names_the_count_the_bytes_and_the_tools`)."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/clearing.py",
        line=133,
        marker="placeholder",
        stable_text="reclaims. Measured against the placeholder's own length",
        why_correct=(
            "Continuation of the previous entry's sentence - the audit's scanner "
            "cuts one hit per LINE, so a two-line comment produces two records "
            "that describe one decision."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/clearing.py",
        line=137,
        marker="placeholder",
        stable_text="The placeholder shape. Short, self-describing",
        why_correct=(
            "The declaration of `PLACEHOLDER_TEMPLATE`. It records that the "
            "placeholder NAMES THE TOOL so the model knows what it could re-run - "
            "which is the difference between a placeholder and a truncation, and "
            "the reason the tool name is in the text at all."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/clearing.py",
        line=139,
        marker="placeholder",
        stable_text="is what makes the transform a placeholder rather than a truncation",
        why_correct=(
            "Second line of the same declaration comment, and it carries the "
            "reason the transform is lossless: it holds NO bytes of the original. "
            "Pinned by `test_the_tool_use_record_survives_the_clearing`."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/clearing.py",
        line=145,
        marker="placeholder",
        stable_text="A message already carrying a placeholder is left alone",
        why_correct=(
            "The comment on `_PLACEHOLDER_RE`. It explains why the regex exists: a "
            "render loop that re-cleared an already-cleared result would inflate "
            "the receipt with a second claim for bytes it did not reclaim. "
            "Implemented, and pinned by "
            "`test_a_cleared_result_is_not_cleared_twice`."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/clearing.py",
        line=181,
        marker="placeholder",
        stable_text="Return the placeholder that replaces a cleared result",
        why_correct=(
            "`placeholder_for`'s one-line docstring. Same domain term as every "
            "other entry in this block."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/clearing.py",
        line=244,
        marker="placeholder",
        stable_text="How many results were replaced by a placeholder",
        why_correct=(
            "`ClearingResult`'s `cleared_count` docstring. Same domain term as "
            "every other entry in this block, and the property it documents - "
            "that the count is of RESULTS REPLACED, not of results examined - is "
            "the one a reader checking the receipt needs."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/conversation.py",
        line=42,
        marker="placeholder",
        stable_text="placeholder, keeping the ``tool_use`` record",
        why_correct=(
            "The module docstring's clearing paragraph, added by P3a. It names "
            "the mechanism AND the two things clearing must not do - drop the "
            "`tool_use` record, or touch a non-re-fetchable result. Both are "
            "enforced in `harness/agent_kernel/clearing.py` and pinned."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/conversation.py",
        line=871,
        marker="placeholder",
        stable_text="Replace old re-fetchable tool results with placeholders, in place",
        why_correct=(
            "`ConversationMemory.clear_tool_results`'s summary line. It says "
            "'in place', which is the load-bearing word: the retained VIEW "
            "changes and the journal does not, which is what makes clearing "
            "reversible. Pinned by `test_the_input_is_not_mutated`."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/conversation.py",
        line=884,
        marker="placeholder",
        stable_text="a result already carrying a placeholder",
        why_correct=(
            "One bullet of the same method's never-clears list, in the module "
            "docstring rather than the body. It exists so a reader who sees the "
            "method signature learns the four exclusions before reading the "
            "implementation; the behaviour is enforced by "
            "`_PLACEHOLDER_RE` in `clearing.py` and pinned by "
            "`test_a_cleared_result_is_not_cleared_twice`."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/conversation.py",
        line=1338,
        marker="not yet",
        stable_text="handoff is not yet counted against the protected region",
        why_correct=(
            "The SAME comment this file already recorded at line 1133 (P1/W2-T1). "
            "P3a inserted into `conversation.py` twice above it and moved it to "
            "1338 - which is the SECOND time one insertion has invalidated this "
            "record, and the reason `stable_text` was added. The comment, the "
            "accounting gap it names, and the classification are all unchanged; "
            "the LINE is the only thing that moved, and it should not have been "
            "load-bearing. The original 1133 entry is kept so the two are visibly "
            "the same decision rather than a silent overwrite."
        ),
    ),
    RecordedDecision(
        file="context_files.py",
        line=488,
        marker="not yet",
        stable_text='"New" means not yet injected for that scope',
        why_correct=(
            "`PathTrigger.pending_for`'s docstring. 'Not yet' is an ordinary "
            "English adverb about trigger state - the same word `steering.py` "
            "uses about queue state - and it is in a `__doc__` describing what "
            "the method filters. The behaviour it describes (idempotence per "
            "scope) is implemented and pinned by "
            "`test_the_second_touch_of_the_same_directory_injects_nothing`."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/conversation.py",
        line=1133,
        marker="not yet",
        stable_text="handoff is not yet counted against the protected region",
        why_correct=(
            "The P1/W2-T1 record, now superseded by the `stable_text`-keyed entry "
            "above. Kept rather than deleted so the history is visible: this line "
            "was 1065, then 1133, and is now 1338, all three times because of an "
            "insertion into this file. `stable_text` exists because a "
            "classification keyed by a line number in a file this heavily pinned "
            "is a record that fails for reasons unrelated to its subject."
        ),
    ),
    RecordedDecision(
        file="agent_kernel/conversation.py",
        # P1/W2-T1: was 1065. The COMMENT this record is about did not move or
        # change meaning - `ConversationMemory.record_image` was inserted above
        # it in the same file, shifting it to 1133. Updating the recorded line is
        # the honest response, and it is worth noting WHY this record is keyed by
        # line at all when P0-W2-T1 deliberately avoided line numbers elsewhere:
        # an insertion into a heavily-pinned shared file silently invalidates a
        # line-keyed classification. The decision is still the same decision.
        line=1133,
        marker="not yet",
        why_correct=(
            "'# handoff is not yet counted against the protected region', in "
            "`_compact`. This is the CLOSEST CALL in the audit: it states a real "
            "accounting gap â€” the handoff message is protected from compaction "
            "but its size is not counted against the protected region's budget â€” "
            "in a comment with no pin. It is classified as a decision because "
            "the comment is TRUE of current behaviour and `harness/AGENTS.md` "
            "(AGT-06) records the follow-up, not because the gap is closed. If "
            "that follow-up is ever dropped, this comment outlives it, so this is "
            "the first entry to re-read when the marker set changes."
        ),
    ),
    RecordedDecision(
        file="deps.py",
        line=188,
        marker="placeholder",
        why_correct=(
            '`"""Placeholder so Callable imports are not flagged; remove me '
            'never."""` on `_unused` â€” a typing import kept alive for a '
            "runtime import cycle. Removing it breaks the cycle-safe import "
            "order, which is why the comment says `remove me never` rather than "
            "`remove me`. The comment is unusually honest; the residual risk is "
            "that a future reader reads only the first clause and deletes the "
            "import, so the whole sentence is quoted here."
        ),
    ),
    RecordedDecision(
        file="knowledge.py",
        line=567,
        marker="placeholder",
        why_correct=(
            "'# placeholder is the same authority `harness.skills` treats it as' "
            "â€” the word is a DOMAIN TERM here (an untrusted-source slot whose "
            "content is not trusted), not a stub for missing code. The line "
            "documents that the slot is governed by the same review policy as a "
            "skill, which is a real constraint and not a to-do."
        ),
    ),
    RecordedDecision(
        file="prompts.py",
        line=307,
        marker="placeholder",
        why_correct=(
            "'memory section (None/empty -> \"(none)\" placeholder)' in "
            "`render_planner_prompt`'s docstring â€” a documentation note that an "
            "EMPTY memory section renders as an explicit `(none)` rather than as "
            "blank space. The render is implemented; the note records that the "
            "`(none)` text is load-bearing, because a planner cannot tell an "
            "absent memory section from a failed one."
        ),
    ),
    RecordedDecision(
        file="redaction.py",
        line=40,
        marker="placeholder",
        why_correct=(
            "'* No secret pattern, no allow-list, no placeholder of our own' â€” a "
            "NEGATIVE declaration in the module docstring. `harness/redaction.py` "
            "deliberately owns no secret vocabulary, because a second "
            "implementation is how two surfaces end up disagreeing about the "
            "same credential. The word appears in the sentence that forbids the "
            "thing a TODO would be asking for."
        ),
    ),
    RecordedDecision(
        file="steering.py",
        line=135,
        marker="placeholder",
        why_correct=(
            "'bare \"replan\" carry a placeholder text (never empty)' in "
            "`parse_steering_line`'s docstring â€” a statement about a deliberate "
            "DEFAULT. The rule is that a steering intent must never be an empty "
            "string, because an empty intent is indistinguishable from a parse "
            "failure and `inject()` treats them differently. The placeholder is "
            "the safe answer and it is implemented."
        ),
    ),
    RecordedDecision(
        file="steering.py",
        # P1/W2-T1: was 997. The COMMENT is unchanged and still says exactly what
        # the `why_correct` below describes; another terminal's insertion above
        # it in this shared tree shifted it to 999. See the note on the
        # `conversation.py` record for why these are keyed by line at all.
        line=999,
        marker="not yet",
        why_correct=(
            "'Record every PENDING seq not yet recorded as queued' in "
            '`observe_arrivals`\'s docstring â€” "not yet" is an ordinary English '
            "adverb about queue STATE, not a marker of unfinished work. This is "
            "the clearest example of why the audit reads each hit rather than "
            "counting them: a regex cannot tell this from the "
            "`conversation.py:1065` comment, and the two deserve opposite "
            "classifications."
        ),
    ),
    RecordedDecision(
        file="trace.py",
        line=8,
        marker="not implemented",
        why_correct=(
            "'Redaction is NOT implemented here. This module used to carry a "
            "private...' â€” an explicit NEGATIVE declaration in the module "
            "docstring. `harness/trace.py` delegates to `harness/redaction.py` "
            "precisely so redaction is implemented in exactly one place. A reader "
            "grepping `NOT implemented` finds the sentence that says redaction is "
            "implemented SOMEWHERE ELSE, which is the opposite of a gap."
        ),
    ),
    RecordedDecision(
        file="trace.py",
        # P1/W2-T1: was 41. Comment unchanged; shifted by an insertion above it
        # in this shared tree. See the `conversation.py` record for the note on
        # line-keyed classifications.
        line=42,
        marker="placeholder",
        why_correct=(
            "'# Historical placeholder emitted by the old private "
            'implementation\' above `LEGACY_REDACTED = "[REDACTED]"` â€” a '
            "compatibility constant kept for a consumer that string-matched the "
            "pre-shared placeholder. It is exported, declared no patterns of its "
            "own, and used on no write path; "
            "`harness/test_secret_egress.py::test_the_context_compiler_does_not"
            "_keep_a_second_redactor` exempts it BY NAME precisely so the "
            "exemption stays visible. A real cleanup risk, not an unfinished "
            "task."
        ),
    ),
    RecordedDecision(
        file="trace.py",
        # P1/W2-T1: was 43. Comment unchanged; shifted with the entry above.
        line=44,
        marker="placeholder",
        why_correct=(
            "'# new code must use the shared placeholder' â€” the instruction that "
            "keeps `LEGACY_REDACTED` from growing a second use. It is the guard "
            "rail for the entry above, and deleting it as a stale comment would "
            "remove the only thing stopping that."
        ),
    ),
)


#: Marker hits that were STALE comments and have been DELETED by this round.
#:
#: **Also empty, and for a stronger reason than `GAPS` is.** Three candidates
#: were read and one was deleted; the two that were kept are recorded above
#: with their reasoning. The deleted one is recorded here rather than simply
#: removed, because a silent comment deletion in a shared tree is indistinguishable
#: from another terminal's in-flight edit â€” which `harness/AGENTS.md` Â§7 records
#: happening twice to `harness/editor.py` in a single round.
STALE_DELETED: Tuple[Tuple[str, int, str, str], ...] = ()


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


def _classified() -> Dict[Tuple[str, int, str], str]:
    """`{key: classification}` for every recorded gap and decision.

    A record carrying ``stable_text`` is ALSO keyed by the identity of every hit
    whose text contains that substring, in that file, with that marker. That is
    what makes a record survive an insertion above it; see
    :attr:`RecordedDecision.stable_text` for why the line-keyed form is not
    enough on its own.
    """
    out: Dict[Tuple[str, int, str], str] = {}
    for gap in GAPS:
        out[(gap.file, gap.line, gap.marker)] = GAP
    for decision in DECISIONS:
        out[(decision.file, decision.line, decision.marker)] = DECISION
    for file, line, marker, _text in STALE_DELETED:
        out[(file, line, marker)] = STALE
    for decision in DECISIONS:
        needle = str(getattr(decision, "stable_text", "") or "")
        if not needle:
            continue
        for hit in HITS:
            if hit.file != decision.file or hit.marker != decision.marker:
                continue
            if needle in hit.text:
                out[(hit.file, hit.line, hit.marker)] = DECISION
    return out


CLASSIFIED = _classified()


def classification_table() -> Dict[str, int]:
    """Per-classification counts, for the round's Handoff."""
    counts = {GAP: 0, DECISION: 0, STALE: 0, "unclassified": 0}
    for hit in HITS:
        counts[CLASSIFIED.get((hit.file, hit.line, hit.marker), "unclassified")] += 1
    return counts


def test_the_audit_found_something_so_it_is_not_vacuous() -> None:
    """A grep that matched nothing satisfies every other assertion here.

    Non-vacuity first, because "no TODO markers in harness/" is exactly what a
    broken scanner reports â€” and the tree has 20+ of them, so a silent
    regression to zero is a real possibility rather than a hypothetical one.
    """
    assert HITS, (
        "no TODO/FIXME/HACK/placeholder markers found in harness/. That is "
        "either true (unlikely â€” the tree has a `todo` tool and several 'not "
        "yet' comments) or the scanner has stopped reading comments. Check that "
        "tokenize still runs before trusting this."
    )
    assert len(HITS) >= 10, (
        f"only {len(HITS)} marker hits; the recorded table below accounts for "
        "more than ten, so the scanner is not finding comments it used to find"
    )


def test_every_marker_hit_in_harness_is_classified() -> None:
    """THE PIN. A new unclassified `TODO` fails here.

    This is the pin the round exists for. An unclassified hit is not a failure of
    the code â€” it is a failure of the RECORD, and the failure message says which
    of the three classes it most likely belongs to so the reader does not have to
    re-derive the analysis.
    """
    unclassified = [
        hit for hit in HITS if (hit.file, hit.line, hit.marker) not in CLASSIFIED
    ]
    assert unclassified == [], (
        f"{len(unclassified)} unclassified marker hit(s) in harness/. Every hit "
        "must be recorded as a GAP (with an owner and an inverted pin), a "
        "DECISION (with the reason it is correct), or a STALE comment (deleted):\n"
        + "\n".join(f"  {hit.as_row()}" for hit in unclassified)
        + "\n\nDo NOT resolve a real gap by implementing it in this wave â€” record "
        "it with an inverted pin instead."
    )


def test_every_recorded_gap_names_an_owner_an_inverted_pin_and_a_reason() -> None:
    """A gap without an owner is the failure `phases/DOCTRINE.md` Â§3 describes.

    Each of the three properties closes a way this table could become a graveyard:

    * an **owner** â€” somebody to hand it to;
    * an **inverted pin** that actually exists in this file, so closing the gap
      breaks a test rather than passing silently;
    * a **why-not-here** â€” so the next reader knows the gap was recorded
      deliberately rather than overlooked.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    assert GAPS, (
        "the gap table is empty. This round recorded one real gap "
        "(harness/test_config.py is a production module named test_*.py, so "
        "pytest collects it and errors on its own public API). If it has been "
        "fixed, delete this table and report the fix. If it moved, record where."
    )
    for gap in GAPS:
        label = f"{gap.file}:{gap.line}"
        assert gap.owner.strip(), f"{label} names no owner"
        assert len(gap.what_is_missing) >= 60, (
            f"{label} describes the gap in {len(gap.what_is_missing)} characters; "
            "a gap nobody can understand is a gap nobody will close"
        )
        assert len(gap.why_not_here) >= 30, (
            f"{label} does not say why it is not closed here"
        )
        assert gap.inverted_pin.startswith("test_"), (
            f"{label} names inverted pin {gap.inverted_pin!r}, which is not a test"
        )
        assert f"def {gap.inverted_pin}(" in source, (
            f"{label} names inverted pin {gap.inverted_pin!r}, which does not "
            "exist in this file. A recorded gap whose pin is missing is an "
            "unrecorded gap with extra steps."
        )


def test_every_recorded_decision_says_why_it_is_correct() -> None:
    """A decision recorded without a reason is a TODO with better manners.

    The failure mode this closes: somebody marks the `todo` tool's name as 'a
    decision' and moves on, and the next reader has no way to check whether it
    still is one. A reason is what makes the classification re-checkable.
    """
    for decision in DECISIONS:
        label = f"{decision.file}:{decision.line}"
        assert len(decision.why_correct) >= 80, (
            f"{label} gives {len(decision.why_correct)} characters of reasoning; a "
            "classification that cannot be re-checked is a classification that "
            "will be wrong someday"
        )


def test_the_test_config_module_collection_error_is_still_there() -> None:
    """Inverted pin for the recorded gap above, and the only way to know it is real.

    A recorded gap whose proof is "I saw it once" is a recorded rumour. This
    RUNS pytest against `harness/test_config.py` and asserts the collection error
    is still present — so the gap is established on every run of this file, not
    on the day somebody noticed it.

    Inverted: when the module is renamed (or the function is), this FAILS and
    says to delete the `GAPS` entry. That direction is deliberate — a gap that
    stops producing a failure the day it is fixed is a gap whose record goes
    stale silently, which is the failure this file exists to prevent.
    """
    import subprocess
    import sys

    # `--collect-only` is NOT enough: pytest resolves a missing fixture at SETUP,
    # so collection succeeds and reports "2 tests collected" while the run still
    # errors. The proof has to be an actual RUN. (That distinction cost this pin
    # its first draft, which passed against a gap that was still open.)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            str(HARNESS_ROOT / "test_config.py"),
        ],
        cwd=str(HARNESS_ROOT.parent),
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = result.stdout + result.stderr
    assert "fixture 'pristine_dir' not found" in combined, (
        "harness/test_config.py no longer produces the collection error, so the "
        "recorded gap is CLOSED (or moved). Delete the GAPS entry and report the "
        "fix — do not simply relax this assertion. GOOD outcome: rename the "
        "module or the function so pytest stops collecting production code."
    )
    # And the gap's SUBJECT must still exist, so the record is about live code.
    source = (HARNESS_ROOT / "test_config.py").read_text(encoding="utf-8-sig")
    assert "def test_config_guard(" in source, (
        "harness/test_config.py no longer defines test_config_guard(); the "
        "recorded collection error has moved or been resolved. Update GAPS in "
        "the same change."
    )


def test_a_declared_decision_is_not_reported_as_a_gap() -> None:
    """Inverted pin: the two tables must stay disjoint.

    An entry in BOTH `GAPS` and `DECISIONS` for the same hit is two competing
    records of one thing, and one of them is wrong â€” which is how a table starts
    telling a reader that unfinished work is a finished decision.
    """
    gap_keys = {(g.file, g.line, g.marker) for g in GAPS}
    decision_keys = {(d.file, d.line, d.marker) for d in DECISIONS}
    overlap = gap_keys & decision_keys
    assert overlap == set(), (
        f"these hits are recorded as BOTH a gap and a decision: {sorted(overlap)}"
    )


def test_the_todo_tool_name_is_not_reported_as_a_gap() -> None:
    """The concrete false-positive this audit had to get right.

    `harness/` contains a tool literally named `todo`, a kernel handler for it,
    and a `SessionState.todo_items` field. A case-insensitive grep for `todo`
    returns all three plus every real marker. If any of the tool's occurrences
    were classified as a gap, the gap count would be wrong and the audit would
    be measuring the vocabulary of the tree rather than its state.
    """
    tool_hits = [h for h in HITS if h.marker == "todo"]
    for hit in tool_hits:
        classification = CLASSIFIED.get((hit.file, hit.line, hit.marker))
        assert classification in (DECISION, None), (
            f"{hit.as_row()} is a `todo` hit; if it is the tool's own name it "
            "must not be recorded as a gap"
        )


def test_a_string_literal_is_not_a_marker_hit() -> None:
    """The scanner reads comments and docstrings, not string data.

    `harness/trace.py` exports `LEGACY_REDACTED = "[REDACTED]"` and several
    modules build `'[journal value truncated: ...]'`. A scanner that read string
    literals would report prose the product emits as a marker, and the gap table
    would fill with phantom entries. Asserted against live code so a future
    change that widens the scanner to literals has to say so on purpose.
    """
    assert any(h.origin == "comment" for h in HITS), (
        "no comment-sourced hits; the tokenize leg of the scanner is dead"
    )
    assert any(h.origin == "docstring" for h in HITS), (
        "no docstring-sourced hits; the AST leg of the scanner is dead"
    )
    trace = (HARNESS_ROOT / "trace.py").read_text(encoding="utf-8-sig")
    assert 'LEGACY_REDACTED = "[REDACTED]"' in trace, (
        "the LEGACY_REDACTED compatibility constant is gone; if the redaction "
        "placeholder wording changed, update DECISIONS and re-run this audit"
    )


def test_the_audit_covers_the_stub_package_too() -> None:
    """`_stubs/` is scanned, because that is where a second implementation hides.

    Wave 1's redaction audit found `harness/context_compiler.py` keeping its own
    redactor with a `# for now` shaped comment. A stub package is where a
    hand-rolled replacement for a real boundary goes, so excluding it would
    exclude the most likely place for the next one.
    """
    assert _STUB_EXCLUDED_FROM_SCAN is False, (
        "_stubs/ was excluded from the audit. Wave 1 found a second redactor in "
        "harness/ precisely because the audit covered a file a reader assumed "
        "was covered."
    )
    scanned = {path.name for path in _harness_sources()}
    stub_names = {path.name for path in (HARNESS_ROOT / "_stubs").glob("*.py")}
    assert stub_names <= scanned, (
        f"these stub modules are not scanned: {sorted(stub_names - scanned)}"
    )
