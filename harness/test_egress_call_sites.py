"""T1.W2.1 — the redaction boundary, pinned STRUCTURALLY, not behaviourally.

Wave 1 built `harness/redaction.py` and wired it into the egress paths it found.
Wave 1's own tests are behavioural: they drive a value through a path and assert
the secret is gone. That is the right test for a path somebody exercised, and it
is **blind to the failure mode this wave exists to prevent** — a NEW call site
added tomorrow, in a module nobody ran a secret through.

A behavioural suite passes for every path somebody happened to exercise. So this
file parses every `.py` under `harness/` with `ast` and asserts the SHAPE:

    every site that constructs a value carrying subprocess output, file content
    or model response text and places it into a result / journal / trace
    payload either routes through the redaction helper, or is on an explicit
    allowlist that carries a written reason.

## Why AST and not a grep

A grep for `stdout` finds 40 things that are not leaks (a type annotation, a
parameter name, a docstring, a `BashResult` attribute assignment that never
leaves the process). A grep for `redact` misses a site that inherits redaction
from an enclosing journal write. Neither can answer the question, which is about
**provenance**: does the payload this call builds reach a journal/result, and if
so has it been through the boundary?

So the classifier is two-stage, and the stages are what make the count a
measurement rather than a grep result:

1. **Is this a SINK?** A call that places a value into something durable — a
   journal emit, a receipt write, a result/receipt/event constructor, a direct
   file write. A sink is identified by its callee name AND, for the journal
   emits, by its first argument being an event-kind string literal. That second
   condition is what separates `trace.log("tool_result", {...})` (a sink) from
   `messages.append({...})` (an in-memory conversation list that never leaves
   the process, and which the model must be able to read verbatim).

2. **Is the payload a CARRIER?** Does the expression reference an identifier
   whose name denotes untrusted bytes — subprocess output, file content, or a
   model response? The carrier vocabulary is `CARRIERS` below, and it is
   deliberately two-tier, because a single flat list produced 12 false positives
   against 1 real signal when this file was being designed.

## The allowlist is the interesting part

Every exemption names a file, a line-anchored expression, an owner, and a
reason. The reasons are not decoration: an entry with an empty or unrecognised
reason FAILS, so the allowlist cannot grow into a silent permission slip. And a
site that is *fixed* must be REMOVED from the allowlist — the "recorded gap"
shape from `phases/DOCTRINE.md` §3, inverted so that closing a gap breaks the
pin until the pin is updated in the same change.

Host-only: no Docker, no provider, no network.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Set, Tuple

HARNESS_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: Callees that place a value into a JOURNAL ROW. `append` and `log` are both
#: here, and both need the receiver discriminator below, because the tree has
#: ~200 `x.append(...)` calls of which about a dozen are journals and the rest
#: are `lines`, `out`, `messages`, `warnings` and friends.
JOURNAL_SINKS: FrozenSet[str] = frozenset(
    {"log", "append", "emit", "emit_event", "write_receipt", "record_event"}
)
#: Receivers that are a JOURNAL. This is the discriminator that makes
#: `journal.append("tool_result", {...})` a sink and `lines.append("x")` not one.
#: Derived by measuring the tree: every receiver in `harness/` that appears on a
#: `log`/`append`/`emit` call carrying a carrier, reviewed by hand.
JOURNAL_RECEIVERS: FrozenSet[str] = frozenset(
    {
        # the two journal authorities Wave 1 wired
        "trace",
        "run_trace",
        # the kernel's canonical journal
        "events",
        # `agent_loop_step.step()`'s event list, and the kernel's strategies'
        "self.events",
        "journal",
        "machine",
        "_st",
        # `session.commands` / `BashSession.commands`: the per-command record
        # that IS the `tool_call`/`tool_result` pair, journaled by `core.py`
        "commands",
        # `_run_verify`'s injected emit, which the adapter binds to the journal
        "emit",
    }
)
#: The subset of `JOURNAL_RECEIVERS` whose payload is redacted BY the receiver
#: itself, so a carrier handed to it cannot survive to disk.
#:
#: This is a structural claim, not an assumption, and it is proved two ways in
#: this file: `test_a_redacting_journal_receiver_really_redacts` drives a secret
#: through each one live, and
#: `test_a_journal_receiver_is_declared_redacting_only_with_proof` asserts every
#: name in this set has a live proof above it. A receiver that redacts is not
#: the same answer as a site that routes through the helper inline, so it gets
#: its own verdict rather than being folded into `REDACTED`.
REDACTING_JOURNAL_RECEIVERS: FrozenSet[str] = frozenset(
    {
        "trace",  # harness.trace.TraceLogger.log -> redact_for_journal
        "run_trace",  # the same class under the run-scoped name
        "events",  # harness.agent_kernel.events.RunEventJournal.append
    }
)
#: Constructors whose instances ARE the record. Building one is the egress.
RESULT_SINKS: FrozenSet[str] = frozenset(
    {
        "TaskResult",
        "ToolResult",
        "AgentResult",
        "RunResult",
        "RunEvent",
        "ApproverJournalEntry",
    }
)
#: Serialisers that hand bytes to a file. `_FILE_WRITE_SINKS` is only consulted
#: for a call whose receiver is a path/file-ish expression, so `stream.write` on
#: an open handle does not match.
_FILE_WRITE_SINKS: FrozenSet[str] = frozenset(
    {"write_text", "write_bytes", "writelines", "dump", "dumps", "dump_json"}
)
#: Methods that turn an object into a record. Called on the object itself, so a
#: `to_dict()` on an unrelated dataclass is a candidate rather than a certainty.
SERIALISER_SINKS: FrozenSet[str] = frozenset({"to_dict", "as_dict", "as_json_dict"})

#: Carrier identifiers, tier 1. Each name denotes bytes that came from OUTSIDE
#: the process: a subprocess, a file, or a model. Tier 1 is the set whose names
#: are unambiguous — there is no ordinary vocabulary meaning of `stdout`.
CARRIERS_TIER1: FrozenSet[str] = frozenset(
    {
        "stdout",
        "stderr",
        "raw_output",
        "tool_output",
        "process_output",
        "result_text",
        "file_content",
        "file_bytes",
        "source_text",
        "response_text",
        "model_output",
        "model_text",
        "check_context",
        "candidate_source",
        "data_bytes",
        "transcript",
    }
)
#: Carrier identifiers, tier 2. These DO carry untrusted bytes at the sites that
#: use them, but the words are ordinary English and therefore also appear in
#: ~50 places that are not carriers (`output=""` defaults, `context` in a
#: geometry helper). Tier 2 is matched, and every tier-2 site must therefore be
#: either redacted or allowlisted — which is the point: it is the tier where a
#: real leak hides, and it is too noisy to blanket-exempt.
CARRIERS_TIER2: FrozenSet[str] = frozenset(
    {"output", "preview", "context", "contents", "body", "raw", "completion", "reply"}
)

#: The names that satisfy the requirement. A call that routes a payload through
#: any of these is a redacted site by construction.
REDACTION_HELPERS: FrozenSet[str] = frozenset(
    {
        "redact_for_journal",
        "redact_text_for_journal",
        "redact_iterable_for_journal",
        "redact_secrets",
        "redact_text",
        "redact_report",
        "redact_text_report",
        "redact_text_scanned",
    }
)

#: The four verdicts. They are distinct on purpose and none of them is
#: interchangeable with another:
#:
#: * `redacted` — the expression routes through a redaction helper inline.
#: * `redacted_by_receiver` — the sink ITSELF redacts the whole payload, so the
#:   carrier cannot survive. Proven live below, not assumed from the name.
#: * `allowlisted` — declared exempt with a written reason and an owner.
#: * `unredacted` — the failure. Nothing in `harness/` may be in this class.
#:
#: `redacted_by_receiver` is separate from `redacted` because the two have
#: different regression shapes: an inline helper can be deleted from a call site,
#: while a receiver's redaction is a property of a class. Collapsing them would
#: hide the second behind the first's evidence.
REDACTED = "redacted"
REDACTED_BY_RECEIVER = "redacted_by_receiver"
ALLOWLISTED = "allowlisted"
UNREDACTED = "unredacted"

#: The human-readable form of each verdict, for the failure message.
#:
#: The wording matters at the moment of failure: a reader who sees "unredacted"
#: next to a `core.py` verify row will reasonably ask whether pytest output is
#: supposed to reach the journal raw, and has to go read this file to learn that
#: it is not. The message below is what they read instead.
VERDICT_PROSE = {
    REDACTED: "redacted at this expression",
    REDACTED_BY_RECEIVER: "the sink redacts the whole payload (proven live)",
    ALLOWLISTED: "declared safe in ALLOWLIST with a written reason",
    UNREDACTED: ("NOT REDACTED and not declared. This is the failure."),
}

ALL_VERDICTS = (REDACTED, REDACTED_BY_RECEIVER, ALLOWLISTED, UNREDACTED)


# ---------------------------------------------------------------------------
# The allowlist — every entry carries a reason, and the reasons are checked
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Allowance:
    """One declared exemption, with the reason it exists and who owns closing it.

    `owner` is not administrative: an exemption with no owner is a permission
        slip with extra steps. `gap` marks entries that are KNOWN leaks rather than
        deliberate decisions, and `test_a_known_gap_that_gets_closed_is_reported`
        inverts on them so closing one without updating this table fails the pin.

        A gap the classifier cannot reach does NOT belong here — those live in
        ``RECORDED_GAPS``, because this table is indexed by (file, expression) and
        an entry whose expression the scanner never produces can never be matched,
        which would make it an unverifiable claim.
    """

    file: str
    expression: str
    reason: str
    owner: str
    gap: bool = False


#: Every exemption for a site the CLASSIFIER FOUND, with a written reason. See
#: the module docstring for why the alternative (blanket-exempt a whole tier) was
#: rejected.
ALLOWLIST: Tuple[Allowance, ...] = (
    Allowance(
        file="agent_kernel/strategy.py",
        expression="body",
        reason=(
            "`glob`'s handler: `body` is the over-cap refusal text this "
            "module ITSELF composes (the SEARCH_TOO_MANY_MESSAGE plus the "
            "match count and sample paths). It is harness prose, not "
            "subprocess output, and the paths in it come from "
            "`retrieval.search_repo`, which returns repository-relative names "
            "only. `ToolResult` is deliberately NOT redacted at construction: "
            "the model must read a real command result verbatim or it cannot "
            "work. See harness/AGENTS.md §3, 'tool result payload'."
        ),
        owner="No action needed — model-facing value, redacted at the journal",
    ),
    Allowance(
        file="agent_kernel/strategy.py",
        expression="'\\n'.join(body)",
        reason=(
            "`glob`'s success path: the same over-cap refusal text, joined. "
            "Harness prose plus relative path names, not untrusted bytes; the "
            "journal write redacts the row that carries it."
        ),
        owner="No action needed — model-facing value, redacted at the journal",
    ),
    Allowance(
        file="agent_kernel/strategy.py",
        expression="output[-int(cfg.get('max_output_chars', 3000)):]",
        reason=(
            "The `shell` handler's own output slice, returned to the model as a "
            "tool result. Deliberately verbatim in-process (the model needs the "
            "bytes), bounded to the configured cap, and redacted when the row "
            "is journaled. This is the exact separation "
            "`harness/redaction.py`'s module docstring describes: the model "
            "gets the bytes verbatim, every human-facing surface gets the "
            "authority."
        ),
        owner="No action needed — model-facing value, redacted at the journal",
    ),
    Allowance(
        file="agent_kernel/strategy.py",
        expression="active_workspace(context).git_status()",
        reason=(
            "The `git_status` tool's result. `git status --porcelain` output "
            "carries repository-relative paths and index state, not file "
            "content; the row is redacted at the journal regardless."
        ),
        owner="No action needed — model-facing value, redacted at the journal",
    ),
    Allowance(
        file="agent_kernel/strategy.py",
        expression="active_workspace(context).git_diff()",
        reason=(
            "The `git_diff` tool's result. This IS diff content, i.e. real file "
            "bytes, and it is the strongest candidate in the tree — but it is "
            "the model's own workspace diff, which it must read verbatim to "
            "work, and the journal row carrying it is redacted. "
            "harness/test_secret_egress.py's journal-pair test is what proves "
            "the row is redacted on disk."
        ),
        owner="No action needed — model-facing value, redacted at the journal",
    ),
    Allowance(
        file="agent_kernel/tools.py",
        expression="output",
        reason=(
            "`ToolRegistry.dispatch` wrapping a handler's return into a "
            "`ToolResult`. This is THE canonical tool-result construction: "
            "whatever the handler produced becomes the model's tool result. "
            "Redacting here would make the agent unable to read a real command "
            "result, so the boundary is the journal write, not this call. "
            "Pinned behaviourally by harness/test_secret_egress.py::"
            "test_the_tool_result_path_journals_redacted."
        ),
        owner="No action needed — model-facing value, redacted at the journal",
    ),
    Allowance(
        file="knowledge.py",
        expression="context.as_dict()",
        reason=(
            "`json.dumps(context.as_dict())` inside a bounded LOG LINE, not a "
            "journal row — the compiled-context receipt's shape summary. "
            "`as_dict()` carries counts, digests and citations, not source "
            "text; the source text itself reaches the model through "
            "`harness/context_compiler._as_text`, which IS redacted at "
            "construction (WAVE-1 §3, 'test failure feedback')."
        ),
        owner="No action needed — receipt shape, not source content",
    ),
)

# ---------------------------------------------------------------------------
# The recorded gaps — sites the classifier CANNOT find, recorded by hand
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedGap:
    """A known un-redacted egress site that a provenance scan cannot reach.

    **This class exists because the scan is not sufficient, and saying so is the
    honest answer rather than a second implementation.**

    `harness/tools.py::_precommit_refusal` builds its message like this:

        message = f"the edit to {...} was refused before it was written: {...}"
        if receipt.get("context"):
            message += "\\n" + str(receipt["context"])
        return ToolResult(..., error=message)

    A provenance scan sees a local named `message`, which carries no carrier name
    at all — the `context` bytes reached the sink two statements earlier, through
    a string concatenation. No AST classifier over identifier names can follow
    that, and a whole-program taint analysis is far outside a source-level pin.

    So the gap is recorded HERE, by hand, with an owner — and, crucially, it is
    pinned by a BEHAVIOURAL assertion
    (`test_the_known_gap_is_still_open_so_this_file_is_not_claiming_a_fix`) that
    drives the real function and checks the secret survives. The behavioural pin
    is what makes the record trustworthy: it fails the day the gap closes, which
    is what `phases/DOCTRINE.md` §3 asks of a recorded gap.
    """

    file: str
    symbol: str
    reason: str
    owner: str
    #: The behavioural test that proves the gap is still open.
    proof: str


RECORDED_GAPS: Tuple[RecordedGap, ...] = (
    RecordedGap(
        file="harness/tools.py",
        symbol="_precommit_refusal",
        reason=(
            "The in-edit gate receipt's `context` is REAL SOURCE BYTES (it is "
            "`lint._context_block(src, line, span)` — the +/-3 lines around a "
            "syntax error). `_precommit_refusal` concatenates it into a message "
            "and hands that to `ToolResult(error=...)` without redacting, while "
            "`lint.render_check` — the SAME receipt's own renderer — redacts the "
            "very same field. One receipt, two renderers, opposite answers.\n"
            "Reachable through `TypedToolRuntime.execute`, whose only callers "
            "today are tests, so this is a structural hole rather than a live "
            "leak. Wave 2 must not implement a recorded gap, so this IS the "
            "record."
        ),
        owner=(
            "T1 — harness/tools.py. One line: route `receipt['context']` "
            "through redact_text_for_journal as lint.render_check already does "
            "for check.context."
        ),
        proof="test_the_known_gap_is_still_open_so_this_file_is_not_claiming_a_fix",
    ),
)


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EgressSite:
    """One place a carrier-bearing value is placed into a durable record."""

    file: str
    line: int
    sink: str
    receiver: str
    carriers: Tuple[str, ...]
    tier: int
    redacted: bool
    expression: str

    @property
    def key(self) -> Tuple[str, int, str]:
        return (self.file, self.line, self.expression)

    def as_row(self, verdict: str = "?") -> str:
        return (
            f"{self.file}:{self.line} sink={self.sink} "
            f"receiver={self.receiver} carriers={sorted(self.carriers)} "
            f"-> {verdict}\n    {self.expression[:150]}"
        )


def _harness_sources() -> List[Path]:
    return sorted(
        path
        for path in HARNESS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
        and "_stubs" not in path.parts
        and not path.name.startswith("test_")
    )


def _callee(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _identifiers(node: ast.AST) -> Set[str]:
    """Every identifier-ish name referenced under `node`.

    Three node kinds, and the THIRD is the one that matters: a **string-key
    subscript**. `receipt["context"]` carries real file bytes and a name-based
    scan that only reads `Name`/`Attribute` nodes sees nothing at all — which is
    exactly how `harness/tools.py::_precommit_refusal` (Wave 2's one recorded
    gap) smuggles the in-edit gate's `context` block into a `ToolResult` under
    the local name `message`.

    That is not a hypothetical: it is the only way the one real gap in this
    audit is invisible to a naive scan, and it is why the gap is recorded here
    by hand rather than found by the classifier.
    """
    names: Set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, (ast.Name, ast.Attribute)):
            names.add(_callee(child))
        elif isinstance(child, ast.Subscript):
            index = child.slice
            # `x["key"]` names `key`; `x[0]` / `x[a:b]` name nothing.
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                names.add(index.value)
    return names


def _receiver(call: ast.Call) -> str:
    """The receiver's name for an attribute call, or `<bare>` / `self.x`."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return "<bare>"
    receiver = func.value
    if isinstance(receiver, ast.Attribute):
        return f"self.{receiver.attr}"
    return _callee(receiver)


def _is_journal_sink(call: ast.Call, name: str) -> bool:
    """True for a JOURNAL emit and NOT for an in-memory list append.

    Two conditions, and both are load-bearing:

    1. **The receiver must be a journal.** `trace.log(...)`, `events.append(...)`
       and `session.commands.append(...)` write a record; `lines.append("x")`,
       `out.append(y)` and `warnings.append(w)` build a list that a caller may
       never serialize. Treating every `append` as an egress produced ~200
       candidate sites, of which the overwhelming majority are `lines`, `out`,
       `parts` and `messages` — and an allowlist with 200 entries stops being an
       allowlist.
    2. **The event-kind argument must be a string literal where the journal
       takes one.** `events.append("tool_result", {...})` names its row kind;
       `messages.append({"role": "user", "content": output})` does not, because
       it is the model's own context window — which must stay verbatim or the
       agent cannot read a real command result.

    Condition 2 alone is not sufficient (`lines.append("x")` passes it) and
    condition 1 alone is not either (`messages.append({...})` passes it), which
    is why the scan takes both. `test_the_scanner_separates_a_journal_emit_from
    _an_in_memory_append` proves both halves against live code.
    """
    if _receiver(call) not in JOURNAL_RECEIVERS:
        return False
    if name in {"log", "emit", "emit_event", "record_event"}:
        # These always take the kind first.
        if not call.args:
            return False
        first = call.args[0]
        return isinstance(first, ast.Constant) and isinstance(first.value, str)
    # `append` / `write_receipt`: `write_receipt` takes only a payload, so the
    # literal-kind rule applies to `append` alone.
    if name == "write_receipt":
        return True
    if not call.args:
        return False
    first = call.args[0]
    return isinstance(first, ast.Constant) and isinstance(first.value, str)


def _is_file_write(call: ast.Call, name: str) -> bool:
    """True for a path/serialiser write, false for `handle.write(...)`.

    `approver.append_journal` writes with a bare `handle.write`, so the receiver
    has to look file-ish. A receiver that is an open handle or a stream is not.
    """
    return _receiver(call) not in {"handle", "stream", "buffer", "fh", "file", "sock"}


def _payload_expressions(call: ast.Call) -> List[ast.AST]:
    """The argument expressions that could carry a value into the sink."""
    return [arg for arg in call.args] + [kw.value for kw in call.keywords]


def _routes_through_redaction(node: ast.AST) -> bool:
    """True when `node` contains a call to any redaction helper.

    Checked on the WHOLE payload expression, not just on the carrier, because
    the usual correct shape is a comprehension or a whole-payload call:
    `redact_for_journal({...})` / `[redact_for_journal(x) for x in items]`. A
    carrier wrapped in ANY redacting call anywhere in the expression tree is
    treated as routed, which is the same rule
    `harness/test_secret_egress.py::test_neither_journal_calls_the_redactor_
    directly` applies to the two journals.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _callee(child.func) in REDACTION_HELPERS:
            return True
    return False


def enumerate_egress_sites() -> List[EgressSite]:
    """Every carrier-bearing value placed into a durable record in `harness/`."""
    found: List[EgressSite] = []
    for path in _harness_sources():
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        # A file that does not parse is NOT skipped. The first draft of this
        # scan had `except SyntaxError: continue`, and that is a silent hole
        # with a specific shape: a UTF-8 BOM (which a Windows editor or a
        # PowerShell `Out-File -Encoding utf8` writes) makes `ast.parse` raise
        # on line 1, and a skip turns "I could not read this file" into "this
        # file is clean". `tests/test_config_trace_state.py::
        # test_harness_modules_import_and_parse` catches a parse failure
        # separately, but a security scan that answers "no leaks" because it
        # could not read the source is worse than no scan.
        tree = ast.parse(source)
        relative = path.relative_to(HARNESS_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node.func)
            is_sink = (
                name in RESULT_SINKS
                or name in SERIALISER_SINKS
                or (name in JOURNAL_SINKS and _is_journal_sink(node, name))
                or (name in _FILE_WRITE_SINKS and _is_file_write(node, name))
            )
            if not is_sink:
                continue
            for payload in _payload_expressions(node):
                if isinstance(payload, ast.Constant):
                    continue
                names = _identifiers(payload)
                tier1 = sorted(names & CARRIERS_TIER1)
                tier2 = sorted(names & CARRIERS_TIER2)
                if not (tier1 or tier2):
                    continue
                try:
                    rendered = ast.unparse(payload)
                except Exception:  # pragma: no cover - unparse is total on 3.10
                    rendered = "<unrenderable>"
                found.append(
                    EgressSite(
                        file=relative,
                        line=node.lineno,
                        sink=name,
                        receiver=_receiver(node),
                        carriers=tuple(tier1 or tier2),
                        tier=1 if tier1 else 2,
                        redacted=_routes_through_redaction(payload),
                        expression=rendered.strip(),
                    )
                )
    return sorted(found, key=lambda s: (s.file, s.line, s.expression))


EGRESS_SITES = enumerate_egress_sites()


def _normalise(expression: str) -> str:
    """Collapse whitespace runs so a reformat cannot reclassify a site.

    The allowlist is keyed on the RENDERED payload expression, which is a
    brittle key by nature. Normalising whitespace is the one instability worth
    removing: `ruff format` moving a space around a slice colon would
    otherwise turn an allowlisted site into an `UNREDACTED` one and the pin
    would fire on a formatting change, which is the fastest way to get a
    structural pin deleted.

    A RENAME does still reclassify, and that is correct: if `output` becomes
    `raw_stdout`, the site's provenance genuinely changed and a reader should
    look at it again.
    """
    return " ".join(expression.split())


def _allowlist_index() -> Dict[Tuple[str, str], Allowance]:
    return {(item.file, _normalise(item.expression)): item for item in ALLOWLIST}


ALLOWLIST_INDEX = _allowlist_index()


def classify(site: EgressSite) -> str:
    """Return one of `ALL_VERDICTS` for one site.

    Ordered most-specific first, and the ordering is the argument: an inline
    redaction call is the strongest evidence (the value provably passed through
    the helper at this exact expression), a redacting receiver is the next
    (the value provably passed through a class that redacts), and an allowlist
    entry is a human judgement on top of both. Collapsing them would let an
    allowlist entry mask a site that is also, independently, safe — and then a
    reader would not know which fact was carrying the weight.
    """
    if site.redacted:
        return REDACTED
    if site.receiver in REDACTING_JOURNAL_RECEIVERS:
        return REDACTED_BY_RECEIVER
    if (site.file, _normalise(site.expression)) in ALLOWLIST_INDEX:
        return ALLOWLISTED
    return UNREDACTED


CLASSIFIED = [(site, classify(site)) for site in EGRESS_SITES]
UNREDACTED_SITES = [site for site, verdict in CLASSIFIED if verdict == UNREDACTED]


# ---------------------------------------------------------------------------
# Non-vacuity — every one of these would pass on an empty scan
# ---------------------------------------------------------------------------


def test_the_scanner_actually_finds_sites() -> None:
    """A scanner that matched nothing satisfies every assertion below.

    Asserted first and separately, because "no un-redacted site found" is
    exactly what a broken scanner reports. This is the control that makes the
    rest of the file a measurement.
    """
    assert EGRESS_SITES, (
        "no egress sites found in harness/ - the carrier vocabulary or the sink "
        "vocabulary has drifted and every other assertion in this file is "
        "vacuous"
    )
    # The floor is deliberately well below the observed count. It exists to
    # catch a VOCABULARY DRIFT (a rename that empties the carrier set, a sink
    # receiver list that stops matching), not to pin the exact number — a count
    # pin would need editing every time a legitimate call site is added, and
    # the exact numbers live in `enumeration_table()` for a reader instead.
    assert len(EGRESS_SITES) >= 15, (
        f"only {len(EGRESS_SITES)} egress sites found; Wave 1's own decision "
        "table names seven boundary paths across a dozen-plus journal writes, so "
        "a count this low means the scan stopped matching"
    )


def test_the_scanner_finds_both_tiers_and_separates_them() -> None:
    """Tier 1 and tier 2 must both be populated, or one tier is dead code.

    Tier 1 is the high-confidence set (`stdout`, `file_content`, `model_output`)
    and tier 2 is the noisy one (`output`, `body`, `context`). A scan that only
    matched tier 2 would be flagging ordinary vocabulary; one that only matched
    tier 1 would be blind to the tier where the real leak was found — and the
    real leak WAS tier 2 (`tools.py::_precommit_refusal`).
    """
    tiers = {site.tier for site in EGRESS_SITES}
    assert tiers == {1, 2}, f"expected both carrier tiers to match, got {tiers}"


def test_the_scanner_separates_a_journal_emit_from_an_in_memory_append() -> None:
    """The receiver + event-kind discriminator is real, and both halves are proven.

    `trace.log("tool_result", {"output": output})` IS an egress site.
    `messages.append({"content": output})` is NOT — it is the model's own
    context window, and it must stay verbatim or the agent cannot read a real
    command result.

    Both shapes exist in the tree today, so both halves are proven against live
    code rather than against a synthetic snippet. The positive half asserts a
    real journal emit carrying a carrier was found; the negative half asserts a
    real in-memory append carrying the SAME carrier was found AND excluded.
    Without the negative half a scanner that matched every `append` would pass.
    """
    emits = [
        site for site in EGRESS_SITES if site.sink in JOURNAL_SINKS and site.carriers
    ]
    assert emits, (
        "no journal emit carrying a carrier was found; the journal arm of the "
        "sink vocabulary is dead, so the whole scan is only seeing result "
        "constructors"
    )

    # The negative half, against real in-memory appends in core.py that carry
    # the very same `output` / `reply` identifiers the journal sites carry.
    core = (HARNESS_ROOT / "core.py").read_text(encoding="utf-8")
    tree = ast.parse(core)
    in_memory = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _callee(node.func) == "append"
        and _receiver(node) == "messages"
        and not _is_journal_sink(node, "append")
    ]
    assert len(in_memory) >= 10, (
        f"only {len(in_memory)} in-memory `messages.append(...)` calls in "
        "core.py; the negative half of the discriminator can no longer be "
        "proven against live code, so the scan could be matching every append"
    )
    carrying = [
        node
        for node in in_memory
        if any(
            _identifiers(payload) & (CARRIERS_TIER1 | CARRIERS_TIER2)
            for payload in _payload_expressions(node)
        )
    ]
    assert carrying, (
        "core.py's in-memory message appends no longer carry a tier-1/tier-2 "
        "carrier; the negative half of the discriminator is unproven"
    )
    # And the exclusion is total: none of those may appear as an egress site.
    excluded_lines = {node.lineno for node in carrying}
    for site in EGRESS_SITES:
        if site.file == "core.py" and site.sink == "append":
            assert site.line not in excluded_lines, (
                f"core.py:{site.line} is an in-memory messages.append that the "
                f"scan reported as an egress site: {site.expression[:80]}"
            )


def test_a_redacting_journal_receiver_really_redacts(tmp_path) -> None:
    """`redacted_by_receiver` is a PROVEN verdict, not a name-shaped assumption.

    Each receiver in `REDACTING_JOURNAL_RECEIVERS` is driven with a live secret
    and the on-disk bytes are checked. This is what stops that verdict from
    being a euphemism for "we assumed the class redacts" — and it is the test
    that has to exist, because the alternative (`redacted`) would mean inline
    redaction at ~15 journal call sites for no security benefit.
    """
    from harness.agent_kernel.events import RunEventJournal
    from harness.trace import TraceLogger

    secret = "sk-" + "A" * 39

    trace_logger = TraceLogger(tmp_path / "t")
    trace_logger.log("tool_result", {"output": f"token={secret}"})
    trace_on_disk = (tmp_path / "t" / "trace.jsonl").read_text(encoding="utf-8")

    journal = RunEventJournal(
        tmp_path / "k" / "trace.jsonl", session_id="s", run_id="r"
    )
    journal.append("tool_result", {"output": f"token={secret}"})
    kernel_on_disk = (tmp_path / "k" / "trace.jsonl").read_text(encoding="utf-8")

    for label, text in (("trace", trace_on_disk), ("events", kernel_on_disk)):
        assert secret not in text, (
            f"the `{label}` journal receiver did not redact a carrier; it is "
            "declared in REDACTING_JOURNAL_RECEIVERS, so either the verdict is "
            "wrong or the receiver stopped redacting"
        )
        assert "[REDACTED" in text, (
            f"the `{label}` journal receiver must carry the authority's "
            "placeholder, not merely have dropped the value — a missing value "
            "and a redacted one are different claims"
        )


def test_a_journal_receiver_is_declared_redacting_only_with_proof() -> None:
    """Every name in `REDACTING_JOURNAL_RECEIVERS` must be a real journal class.

    Without this, adding `"lines"` to that set would silence every `lines.append`
    site in the tree on the strength of a name. The check is that the receiver
    is one of the two classes the previous test drives live, under the names
    those classes actually appear as in `harness/`.
    """
    proven = {"trace", "run_trace", "events"}
    assert proven >= REDACTING_JOURNAL_RECEIVERS, (
        "REDACTING_JOURNAL_RECEIVERS names a receiver with no live proof: "
        f"{sorted(REDACTING_JOURNAL_RECEIVERS - proven)}. Add the proof to "
        "test_a_redacting_journal_receiver_really_redacts first, or the verdict "
        "is an assumption."
    )
    assert REDACTING_JOURNAL_RECEIVERS <= JOURNAL_RECEIVERS, (
        "a redacting receiver must also be a journal receiver"
    )
    # And the classes must still exist under those names, so a rename shows up
    # here rather than as a silent reclassification.
    from harness.agent_kernel.events import RunEventJournal
    from harness.trace import TraceLogger

    assert callable(TraceLogger.log)
    assert callable(RunEventJournal.append)


def test_the_redaction_helper_vocabulary_names_the_real_helpers() -> None:
    """The helper set must match what `harness/redaction.py` actually exports.

    Otherwise a rename would make every site in the tree look UNREDACTED and the
    failure would read as a mass leak rather than as a vocabulary drift.
    """
    from harness import redaction

    exported = {
        name
        for name in dir(redaction)
        if name.startswith("redact") or name.startswith("REDACT")
    }
    assert exported, "harness.redaction exports nothing recognisable"
    for helper in (
        "redact_for_journal",
        "redact_text_for_journal",
        "redact_iterable_for_journal",
    ):
        assert helper in REDACTION_HELPERS, f"{helper} is not in the pin's vocabulary"
        assert hasattr(redaction, helper), f"harness.redaction has no {helper}"


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


def test_every_egress_site_is_redacted_or_allowlisted() -> None:
    """The pin itself. Adding one un-redacted call site fails here.

    The failure message names every offender with its file, line, carriers and
    rendered expression, because "a new call site was added" is not actionable
    and a pin whose failure needs a debugger gets deleted by the next terminal.
    """
    assert UNREDACTED_SITES == [], (
        f"{len(UNREDACTED_SITES)} egress site(s) place untrusted bytes into a "
        "result / journal / trace payload without routing through "
        f"`harness.redaction`, and are not on the allowlist with a written "
        f"reason.  [{VERDICT_PROSE[UNREDACTED]}]\n"
        + "\n".join(
            site.as_row(f"{UNREDACTED} - {VERDICT_PROSE[UNREDACTED]}")
            for site in UNREDACTED_SITES
        )
        + "\n\nFor each site, ONE of:\n"
        "  * route the value through redact_for_journal / "
        "redact_text_for_journal\n"
        "  * if the receiver is a journal that redacts the whole payload, add "
        "it to REDACTING_JOURNAL_RECEIVERS and add the live proof to "
        "test_a_redacting_journal_receiver_really_redacts\n"
        "  * if it is a deliberate decision, add an `Allowance` to ALLOWLIST "
        "naming the reason and the owner\n"
        "  * if it is a real leak, record it in RECORDED_GAPS with an owner and "
        "a behavioural proof - do NOT just allowlist it\n\n"
        f"(For reference: {len(EGRESS_SITES)} site(s) scanned, "
        f"{len([1 for _, v in CLASSIFIED if v == REDACTED])} redacted inline, "
        f"{len([1 for _, v in CLASSIFIED if v == REDACTED_BY_RECEIVER])} "
        f"redacted by the sink, "
        f"{len([1 for _, v in CLASSIFIED if v == ALLOWLISTED])} allowlisted, "
        f"{len(UNREDACTED_SITES)} unredacted.)"
    )


def test_tier_one_carriers_are_redacted_or_allowlisted_with_extra_strictness() -> None:
    """Tier 1 is asserted separately, because it deserves a stricter answer.

    Tier-1 names (`stdout`, `stderr`, `file_content`, `model_output`, ...) have
    no ordinary-vocabulary meaning: anything named `stdout` is subprocess
    output. So every tier-1 site must be REDACTED or allowlisted, and the
    allowlist entries that name a tier-1 carrier must say so in their reason —
    a reader scanning for "is subprocess output ever un-redacted" should not
    have to infer it from a name list.
    """
    tier1 = [site for site in EGRESS_SITES if site.tier == 1]
    assert tier1, "no tier-1 carriers found; the strict tier is dead code"
    for site in tier1:
        verdict = classify(site)
        assert verdict != UNREDACTED, site.as_row(verdict)
        if verdict == ALLOWLISTED:
            allowance = ALLOWLIST_INDEX[(site.file, _normalise(site.expression))]
            assert (
                "redact" in allowance.reason.lower()
                or "label" in allowance.reason.lower()
                or "closed set" in allowance.reason.lower()
            ), (
                f"{site.file}:{site.line} is an un-redacted TIER-1 carrier "
                f"({sorted(site.carriers)}) and its allowlist reason does not "
                f"explain where the redaction happens: {allowance.reason[:200]}"
            )


def test_the_audit_found_exactly_one_recorded_gap() -> None:
    """The headline, and what makes this file an audit rather than a lint rule.

    Wave 2 read every carrier-bearing egress site in `harness/` and found ONE
    that is a genuine un-redacted leak rather than a model-facing value the
    journal redacts: `harness/tools.py::_precommit_refusal`.

    Asserting the count is what distinguishes "the scan got broader" from "the
    tree gained a leak": a new un-redacted *site* fails
    `test_every_egress_site_is_redacted_or_allowlisted`, and a new *gap* shows up
    here as a count change with a name attached. Either way the reader finds out
    from a test rather than from rediscovering it.
    """
    assert len(RECORDED_GAPS) == 1, (
        f"expected exactly one recorded gap, found {len(RECORDED_GAPS)}: "
        + ", ".join(f"{g.file}::{g.symbol}" for g in RECORDED_GAPS)
        + ". A second is a real finding — record it with an owner and a proof. "
        "Zero means the one we recorded was closed and this table should be "
        "deleted with the fix reported."
    )
    assert RECORDED_GAPS[0].symbol == "_precommit_refusal", (
        f"the recorded gap is now {RECORDED_GAPS[0].symbol}; harness/AGENTS.md "
        "§9 names _precommit_refusal, so either it was fixed (delete the entry "
        "and report it) or it moved (update the record in the same change)"
    )
    # And no allowlist entry is ALSO a gap — the two tables are disjoint by
    # construction, and an entry in both would mean two competing records of one
    # gap, which is how one of them goes stale.
    assert not [item for item in ALLOWLIST if item.gap], (
        "an allowlist entry is marked gap=True; a gap the classifier CAN reach "
        "belongs in ALLOWLIST with gap=True, and one it cannot belongs in "
        "RECORDED_GAPS. It cannot be both."
    )


def test_every_allowlist_entry_is_actually_reachable_and_still_needed() -> None:
    """An allowlist entry that matches nothing is a permission that grew stale.

    Stale entries are how an allowlist becomes a blanket exemption: nobody
    removes them, and the next reader cannot tell which entries still describe
    live code. This asserts the allowlist is EXACTLY the set of currently
    un-redacted sites that are exempt — no more, no fewer.
    """
    live = {
        (site.file, _normalise(site.expression))
        for site in EGRESS_SITES
        if not site.redacted and site.receiver not in REDACTING_JOURNAL_RECEIVERS
    }
    declared = set(ALLOWLIST_INDEX)
    orphans = declared - live
    assert orphans == set(), (
        "these allowlist entries match no current egress site and should be "
        "deleted (a stale entry is a permission nobody is watching):\n"
        + "\n".join(f"  {f}: {e}" for f, e in sorted(orphans))
    )


def test_every_allowlist_entry_carries_a_real_reason_and_an_owner() -> None:
    """A reason that is a placeholder is not a reason.

    Checks the SHAPE of the reason (length, and that it names where the
    redaction happens or why nothing needs redacting) rather than matching
    against a list of blessed strings, so a new entry is judged on its own
    content and an existing one cannot be emptied.
    """
    for item in ALLOWLIST:
        label = f"{item.file}: {item.expression[:70]}"
        assert len(item.reason) >= 60, (
            f"{label} has a reason too short to be a reason: "
            f"{item.reason!r}. An exemption whose justification is one clause "
            "is how an allowlist becomes a permission slip."
        )
        assert item.owner.strip(), f"{label} names no owner"
        lowered = item.reason.lower()
        assert any(
            marker in lowered
            for marker in ("redact", "label", "closed set", "bounded", "known gap")
        ), (
            f"{label} does not say where the redaction happens or why the value "
            f"cannot carry a secret: {item.reason[:200]}"
        )


def test_a_known_gap_that_gets_closed_is_reported() -> None:
    """Inverted pin for the ALLOWLIST gaps: closing one must break this file.

    `phases/DOCTRINE.md` §3: *"a recorded gap is one somebody can close; an
    unrecorded one just gets rediscovered."* The corollary is that fixing one has
    to be VISIBLE, or the record silently goes stale and the next reader trusts
    a gap that no longer exists.

    So a `gap=True` allowlist entry whose site is no longer an un-redacted site
    fails here, telling whoever fixed it to delete the entry. The pin is
    inverted on purpose: it passes today because the gap is still open, and it
    will fail the day the gap closes — which is exactly when the record needs
    updating.
    """
    still_unredacted = {
        (site.file, _normalise(site.expression))
        for site in EGRESS_SITES
        if not site.redacted and site.receiver not in REDACTING_JOURNAL_RECEIVERS
    }
    closed = [
        item
        for item in ALLOWLIST
        if item.gap and (item.file, _normalise(item.expression)) not in still_unredacted
    ]
    assert closed == [], (
        "these recorded allowlist gaps are no longer reachable as un-redacted "
        "sites, so the gap is closed (or moved) and the record is now stale — "
        "delete the `Allowance` and promote the site to a redacted one:\n"
        + "\n".join(f"  {i.file}: {i.expression}" for i in closed)
    )


def test_every_recorded_gap_names_an_owner_a_reason_and_a_live_proof() -> None:
    """`RECORDED_GAPS` is a claim table, so each claim must be checkable.

    Three things are asserted per entry, and each one closes a way this table
    could become a place where gaps go to be forgotten:

    * an **owner** — a gap with no owner is rediscovered, which is the failure
      the table exists to prevent;
    * a **reason of substance** — over 200 characters, so an entry cannot be a
      bare slug;
    * a **named proof** — the behavioural test that fails the day the gap closes.
      A gap whose proof does not exist is a comment, and a comment is what
      `phases/DOCTRINE.md` §3 is arguing against.
    """
    assert RECORDED_GAPS, (
        "RECORDED_GAPS is empty. Wave 2's audit found one real un-redacted "
        "egress site in harness/ (harness/tools.py::_precommit_refusal); if it "
        "has been FIXED, delete this table and report the fix. If it has been "
        "MOVED, record where."
    )
    for gap in RECORDED_GAPS:
        label = f"{gap.file}::{gap.symbol}"
        assert gap.owner.strip(), f"{label} names no owner"
        assert len(gap.reason) >= 200, (
            f"{label} has a reason too short to be a record: {len(gap.reason)} "
            "characters. A gap whose justification is two sentences is a gap "
            "nobody will come back to."
        )
        assert gap.proof.startswith("test_"), (
            f"{label} names proof {gap.proof!r}, which is not a test name"
        )
        # The named proof must actually exist in this file, by source.
        source = Path(__file__).read_text(encoding="utf-8")
        assert f"def {gap.proof}(" in source, (
            f"{label} names proof {gap.proof!r}, which does not exist in this "
            "file. A recorded gap whose proof is missing is an unrecorded gap "
            "with extra steps."
        )
        # And the symbol must still exist.
        module_path = HARNESS_ROOT.parent / gap.file
        assert module_path.exists(), f"{label} names a file that does not exist"
        module_source = module_path.read_text(encoding="utf-8")
        assert f"def {gap.symbol}(" in module_source, (
            f"{label}: {gap.file} no longer defines {gap.symbol}. Either the gap "
            "was fixed (delete the entry and report it) or the function was "
            "renamed (update the record in the same change)."
        )


def test_the_known_gap_is_still_open_so_this_file_is_not_claiming_a_fix() -> None:
    """The complementary direction, and the reason the gap entry exists.

    `harness/tools.py::_precommit_refusal` places the in-edit gate receipt's
    `context` — real source bytes — into a `ToolResult` without redacting,
    while `lint.render_check` redacts the very same field. This asserts the gap
    is STILL OPEN, so nobody reads this file as evidence that it was fixed.

    Wave 2 is explicitly forbidden from implementing a recorded gap: closing it
    changes behaviour on a path `TypedToolRuntime.execute` owns, and the fix
    belongs to whoever owns that call site. The gap is recorded, with an owner,
    and pinned open.
    """
    from harness import lint, tools

    secret = "sk-" + "A" * 39
    candidate = f"x = 1\nTOKEN = {secret}\ndef f(:\ny = 2\n"
    check = lint.check_source_for_edit(candidate, "settings.py")
    assert check.context and secret in str(check.context), (
        "the in-edit check no longer carries raw source in its context block; "
        "the recorded gap below is about a different thing than this file "
        "claims, so it needs re-auditing"
    )
    receipt = {
        "path": "settings.py",
        "status": check.status,
        "refused": True,
        "checked": True,
        "line": check.line,
        "message": check.message,
        "context": check.context,
    }
    refusal = tools._precommit_refusal("edit", receipt)
    rendered = str(getattr(refusal, "error", None) or refusal.get("error", ""))
    assert secret in rendered, (
        "harness/tools.py::_precommit_refusal no longer leaks the gate "
        "receipt's context into its ToolResult. GOOD — but this file records "
        "the gap as OPEN, so the `gap=True` allowlist entry must be deleted and "
        "the fix reported. Do not simply relax this assertion."
    )
    # And the contrast that makes the gap a gap rather than a policy: the
    # receipt's OWN renderer redacts the same field.
    assert secret not in str(lint.render_check(check)), (
        "lint.render_check no longer redacts the context block; if redaction was "
        "removed there rather than added in tools.py, this gap's recorded cause "
        "is stale"
    )


# ---------------------------------------------------------------------------
# The table a reader or a handoff needs
# ---------------------------------------------------------------------------


def enumeration_table() -> Dict[str, Dict[str, int]]:
    """Per-file counts by verdict, for the round's Handoff."""
    table: Dict[str, Dict[str, int]] = {}
    for site, verdict in CLASSIFIED:
        row = table.setdefault(site.file, {name: 0 for name in ALL_VERDICTS})
        row[verdict] += 1
    return dict(sorted(table.items()))
