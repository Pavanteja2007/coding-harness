"""P0/W2 T4.W2.2 — the render-path pin, and the sanitize order pinned in SOURCE.

**What this file is for.** A behavioural test passes for every render path
somebody happened to exercise. The failure mode this round exists to prevent is
a **new** render path that skips the sanitiser — a path that does not exist yet,
so no behavioural test covers it and none will until someone is hurt by it.

So the pin is structural: it parses every `.py` under `cli/`, finds every call
that writes to a display sink, and demands the value expression be either
(a) produced by the single sanitise helper, or (b) on a written allowlist that
says what content that path carries and why it does not need the sanitiser.

**Three gates, and why there are three.**

1. `test_every_render_site_is_classified` — the coverage gate. A new render
   site in a new function is red until somebody says what it renders.
2. `test_the_allowlist_has_no_stale_rows` — the honesty gate. An allowlist row
   for a function that no longer exists is a claim about the product that is
   no longer true, and a dead allowlist row is where the next exemption goes to
   hide. This is the shrink-only idiom `tests/test_module_reachability.py`
   already uses for the same reason.
3. `TestTheUntrustedRenderPathsArePinned` — the content gate. Not every render
   site carries hostile content, and pretending otherwise would make this file a
   list of 102 excuses. This gate names the paths that DO carry untrusted
   content and requires each of them to reach the sanitiser.

**The order pin.** `cli/ui.py::sanitize_text` must strip escapes BEFORE it
redacts, and that is asserted by reading the function's AST — the line number of
the strip call against the line number of the redact call — not by observing an
outcome. On a tree where the authority has grown tolerant of ANSI inside a
token, every behavioural shape test would still pass with the order inverted,
because the strip that runs afterwards would reassemble a credential out of the
pieces the redactor declined to match. A refactor that inverts the order has to
fail HERE.

**Demonstrated, not asserted.** `test_the_pin_fails_on_a_deliberately_raw_render_site`
writes a temporary module into `cli/` containing a render site that hands a
journal value straight to `print`, runs this file's own gate against it, and
removes it. The transcript is in this round's handoff; the test is the receipt.

Host-only: no Docker, no provider, no network, no credential.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Tuple

CLI_DIR = Path(__file__).resolve().parent
UI_PATH = CLI_DIR / "ui.py"

# ---------------------------------------------------------------------------
# The sink vocabulary. Declared here so a reader can see the whole definition
# rather than infer it, and so a NEW sink is an edit to this table that a
# reviewer can see in the diff.
# ---------------------------------------------------------------------------

#: Constructors that produce a DISPLAY sink. A name bound to one of these is a
#: receiver; the bound set is derived per module so `cli/main.py`'s `console`
#: and a local `Console()` in `cli/doctor.py` are both recognised without a
#: name list.
SINK_CONSTRUCTORS = frozenset(
    {
        "Console",
        "RichLog",
        "TextLog",
        "Log",
        "Table",
        "Tree",
        "Static",
        "Label",
        "Panel",
        "Markdown",
        "Pretty",
    }
)

#: Parameter names that mean "this is an injected display sink". A console is
#: passed into `interactive.run_interactive` and `serve.acp_stdio_serve`; the
#: receiver-binding pass above cannot see that, so the parameter list can.
SINK_PARAMETERS = frozenset(
    {
        "con",
        "console",
        "log",
        "rich_log",
        "sink",
        "out",
        "stream",
        "target",
        "rich",
        "rich_console",
        "live",
        "screen",
    }
)

#: Methods that WRITE to a sink. `update`/`write`/`add_*` are here because a
#: `Static.update()` and a `RichLog.write()` both draw, and a diff pane is
#: updated rather than printed.
SINK_METHODS = frozenset(
    {
        "print",
        "log",
        "write_line",
        "notify",
        "bell",
        "update",
        "update_line",
        "add_row",
        "add_entries",
        "add_files",
        "write",
    }
)

#: Methods a shell class defines to push a line at the user. They are sinks by
#: being the display path, whatever they happen to be named.
SELF_SINK_METHODS = frozenset(
    {
        "transcript",
        "say",
        "say_line",
        "print_line",
        "emit",
        "write_line",
        "notify",
        "bell",
        "add_line",
        "set_text",
    }
)

#: Clipboard writes. A copy is not a render, but it moves the same bytes to a
#: different process, and `cli/session.py::copy_text_to_clipboard` shells out.
CLIPBOARD_FUNCTIONS = frozenset(
    {
        "copy_text_to_clipboard",
        "copy_to_clipboard",
        "pyperclip_copy",
        "write_clipboard",
    }
)

#: The ONE sanitise entry point every render path is supposed to reach.
SANITISERS = frozenset({"sanitize_text", "strip_ansi", "strip_escapes"})

#: Markup escapers. Escaping is NOT sanitising — a `[bold]` in a repository path
#: deletes the message between the tags, and `sk-...` in a pytest excerpt is
#: still a credential on the clipboard. A site that only escapes is classified
#: `escaped`, never `sanitised`, so the two cannot be confused.
ESCAPERS = frozenset(
    {
        "escape",
        "escape_lines",
        "safe_lines",
        "markup_safe",
        "safe_text",
        "text_lines",
        "rich_text",
    }
)

#: Structure-only conversions. These change a value's type or shape and cannot
#: introduce content that was not already in their argument.
STRUCTURE_ONLY = frozenset(
    {
        "str",
        "int",
        "float",
        "len",
        "sorted",
        "repr",
        "type",
        "bool",
        "round",
        "abs",
    }
)


# ---------------------------------------------------------------------------
# The analysis. Kept in this file, not shared, because a gate whose verdict can
# be reached from another module is a gate a caller can argue with.
# ---------------------------------------------------------------------------


class RenderSite:
    """One call that writes to a display sink, and what it writes."""

    __slots__ = ("clean", "function", "lineno", "method", "module", "source")

    def __init__(
        self,
        module: str,
        function: str,
        lineno: int,
        method: str,
        source: str,
        clean: "set | None" = None,
    ):
        self.module = module
        self.function = function
        self.lineno = lineno
        self.method = method
        self.source = source
        #: Names bound in the enclosing function to a sanitiser-derived value.
        #: Carried on the SITE rather than recomputed, because the site's value
        #: expression is usually a bare ``Name`` — `print(value)` after
        #: `value = sanitize_text(...)` — and classifying the name alone would
        #: call a correctly-sanitised render site `declared`. That bug was found
        #: by this round's own positive control, not by reading the code.
        self.clean = clean or set()

    @property
    def key(self) -> Tuple[str, str]:
        return (self.module, self.function)

    @property
    def where(self) -> str:
        return f"{self.module}:{self.lineno} {self.function}() -> {self.method}(...)"

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<RenderSite {self.where} {self.source!r}>"


def _is_sink_constructor(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name in SINK_CONSTRUCTORS


def _bound_sink_receivers(tree: ast.AST) -> set:
    """Names and attributes bound to a display sink anywhere in the module.

    Derived, not declared, so a module that starts constructing its own console
    is recognised without anybody updating a list.
    """
    bound: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(value, ast.Call) and _is_sink_constructor(value):
                for target in targets:
                    if isinstance(target, ast.Name):
                        bound.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        bound.add(target.attr)
        elif isinstance(node, ast.withitem) and isinstance(
            node.optional_vars, ast.Name
        ):
            if node.context_expr is not None and _is_sink_constructor(
                node.context_expr
            ):
                bound.add(node.optional_vars.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                if arg.arg in SINK_PARAMETERS:
                    bound.add(arg.arg)
    return bound


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _called_names(node: ast.AST, names: frozenset) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _callee_name(sub) in names:
            return True
    return False


def _sanitiser_bound_names(fn: ast.AST) -> set:
    """Names bound INSIDE this function to a sanitiser-derived expression.

    One hop, on purpose. A transitive "does this function call a sanitiser
    somewhere" closure was measured during this round's design and it classified
    256 functions as sanitiser-derived, including `_execute_task` — a 2,000-line
    function that contains one `strip_ansi` call somewhere in its body and is
    therefore not a render path anyone could reason about. One hop is the most
    precision this gate can carry.
    """
    clean: set = set()
    for node in ast.walk(fn):
        targets: list = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            value, targets = node.value, node.targets
        elif isinstance(node, ast.AnnAssign):
            value, targets = node.value, [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            value, targets = node.iter, [node.target]
        elif isinstance(node, ast.withitem):
            if node.optional_vars is None:
                continue
            value, targets = node.context_expr, [node.optional_vars]
        elif isinstance(node, ast.NamedExpr):
            value, targets = node.value, [node.target]
        else:
            continue
        if value is None or not _called_names(value, SANITISERS):
            continue
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Name):
                    clean.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    clean.add(sub.attr)
    return clean


def _is_render_call(call: ast.Call, bound: set) -> bool:
    name = _callee_name(call)
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    if name in CLIPBOARD_FUNCTIONS or name == "print":
        return True
    if name in SELF_SINK_METHODS and isinstance(receiver, ast.Attribute):
        return True
    if name in SINK_METHODS:
        if isinstance(receiver, ast.Name) and receiver.id in bound:
            return True
        if isinstance(receiver, ast.Attribute) and receiver.attr in bound:
            return True
        if isinstance(receiver, ast.Call) and _is_sink_constructor(receiver):
            return True
    return False


def render_sites(module_paths: "List[Path] | None" = None) -> List[RenderSite]:
    """Every display-sink call under ``cli/``, with its first value expression."""
    paths = module_paths or sorted(CLI_DIR.glob("*.py"))
    found: List[RenderSite] = []
    for path in paths:
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        bound = _bound_sink_receivers(tree)
        for fn in [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            clean = _sanitiser_bound_names(fn)
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                if not _is_render_call(node, bound):
                    continue
                found.append(
                    RenderSite(
                        module=path.name,
                        function=fn.name,
                        lineno=node.lineno,
                        method=_callee_name(node),
                        source=ast.unparse(node.args[0]),
                        clean=clean,
                    )
                )
    return found


def _value_is_literal(expr: ast.AST) -> bool:
    """True when the expression tree contains no non-constant leaf.

    A constant cannot carry a secret from anywhere else, so a literal render
    site is safe by construction rather than by review. That is the difference
    between this being a real reduction and this being a disguised allowlist.
    """
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant):
            continue
        if isinstance(node, ast.JoinedStr):
            if not all(isinstance(v, ast.Constant) for v in node.values):
                return False
            continue
        if isinstance(node, (ast.Load, ast.operator, ast.unaryop, ast.expr_context)):
            continue
        return False
    return True


def classify(site: RenderSite) -> str:
    """``literal`` | ``sanitised`` | ``escaped`` | ``declared`` for one site.

    Four verdicts, and the distinction between the middle two is the point:
    ``sanitised`` means the credential is gone, ``escaped`` means only the
    MARKUP is neutralised. A site that escapes a pytest excerpt containing
    ``sk-...`` is safe to read and unsafe to copy, and calling that ``sanitised``
    would let the next person think the credential was removed.
    """
    if site.source == "":
        return "literal"
    try:
        expr = ast.parse(site.source, mode="eval").body
    except SyntaxError:  # pragma: no cover - ast.unparse always round-trips
        return "declared"
    if _value_is_literal(expr):
        return "literal"
    if _value_is_clean(expr, site.clean, SANITISERS):
        return "sanitised"
    if _value_is_clean(expr, site.clean, ESCAPERS):
        return "escaped"
    return "declared"


def _value_is_clean(expr: ast.AST, clean: set, producers: frozenset) -> bool:
    """Every leaf of ``expr`` is constant, a declared producer, or bound to one.

    Recursive rather than a single ``_called_names`` because the interesting
    case is an f-string: ``print(f"  {journal_value}")`` where
    ``journal_value = sanitize_text(...)`` is the CORRECT pattern and a
    whole-expression substring search misses it. It found the gap by being run
    against this round's own positive control, which is what a positive control
    is for.

    Every formatted value must be clean, not any of them: ``f"{safe} {raw}"`` is
    exactly the leak this gate exists to catch, and an ``any()`` would pass it.
    """
    if expr is None:
        return False
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, ast.JoinedStr):
        return all(_value_is_clean(v, clean, producers) for v in expr.values)
    if isinstance(expr, ast.FormattedValue):
        return _value_is_clean(expr.value, clean, producers)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _value_is_clean(expr.left, clean, producers) and _value_is_clean(
            expr.right, clean, producers
        )
    if isinstance(expr, ast.Name):
        return expr.id in clean
    if isinstance(expr, ast.Attribute):
        return expr.attr in clean
    if isinstance(expr, ast.Subscript):
        return _value_is_clean(expr.value, clean, producers)
    if isinstance(expr, ast.Call):
        name = _callee_name(expr)
        if name in producers:
            return True
        if name in STRUCTURE_ONLY:
            return all(_value_is_clean(a, clean, producers) for a in expr.args)
        return False
    if isinstance(expr, (ast.Tuple, ast.List)):
        return bool(expr.elts) and all(
            _value_is_clean(e, clean, producers) for e in expr.elts
        )
    return False


# ---------------------------------------------------------------------------
# THE ALLOWLIST — one written reason per (module, function).
#
# Every row says what that path's interpolated values ARE, because "it is fine"
# is not a reason and "no untrusted content" is only useful if somebody can
# check it. The reasons name the concrete producer. This table is the only
# place in `cli/` where a render site may be un-classified, and adding a row is
# a reviewable diff rather than a silent widening.
# ---------------------------------------------------------------------------

DECLARED_RENDER_FUNCTIONS: Dict[Tuple[str, str], str] = {
    ("interactive.py", "_render_review"): (
        "The `/review` renderer. Both branches route through `ui.sanitize_text` "
        "AND `escape` -- including the degraded `except` branch, which is the "
        "VEX-TERM-UX-09 blocker that printed `body[:4000]` raw. The rows that "
        "still classify as undeclared are the SURROUNDING chrome: the heading, "
        "the rationale label and the action lines."
    ),
    ("interactive.py", "_render_subcommand_receipt"): (
        "The subcommand receipt renderer. The undeclared values are the verb and "
        "the status word, both drawn from the registry's own closed vocabulary "
        "rather than from content."
    ),
    ("main.py", "cmd_watch"): (
        "`neo watch` progress rows: the follower's own state words, a byte "
        "offset and a task id. No journal CONTENT is on this path; the rendered "
        "projection is the one the follower writes to a file."
    ),
    ("_render_pin_demo.py", "_report"): (
        "The AST-pin demonstration driver's own report lines. Every value is a "
        "count it just measured or a (module, function) key it derived from the "
        "scan itself, printed for a human reading the transcript."
    ),
    ("_render_pin_demo.py", "main"): (
        "The demonstration's verdict block: four integers this script just "
        "computed and a fixed word, printed so the before/after is readable "
        "without reading the test."
    ),
    ("__main__.py", "_run"): (
        "The package entry point's top-level failure line. The interpolated "
        "value is the exception TYPE of a failed `cli` import, so it names a "
        "class and carries no file content."
    ),
    ("auth.py", "connect_interactive"): (
        "`/connect` receipt rows assembled by `cli.auth.render_connect_result`. "
        "The values are provider DISPLAY names from `PROVIDERS` and the masked "
        "projection `Credential.to_dict()`, which cannot hold a literal key."
    ),
    ("auth.py", "cmd_auth_logout"): (
        "`/logout` receipt. Provider ids come from the credential store's own "
        "keys and `markup_safe` wraps the one that is echoed back; the masked "
        "projection is what the row renders, not the store row."
    ),
    ("command_import.py", "register_adopt_parser"): (
        "The `/adopt` argparse handler. Its rows are a `json.dumps` machine "
        "document and the `receipt.lines` the module's own renderer already "
        "escaped through `escape_lines`."
    ),
    ("command_import.py", "_handler"): (
        "The `/adopt` REPL dispatcher, same payload as the script handler above: "
        "a JSON document and a receipt whose lines the module escaped."
    ),
    ("commands.py", "cmd_worktree"): (
        "`neo worktree` report rows. The values are git's own `node_id`, "
        "`base_commit` and `path` fields from `runtime.worktrees`, rendered as a "
        "receipt or a JSON document for a script."
    ),
    ("completion.py", "cmd_completions"): (
        "The hidden `__completions` backend. Every row is a candidate NAME taken "
        "from the live argparse tree — subcommand, flag or choice — and it is "
        "written to stdout for a shell to read, one per line."
    ),
    ("completion.py", "install_completion"): (
        "`neo completion --install` report rows. The values are the install "
        "path, the shell name and the `$PROFILE` path this module computed, "
        "plus the fixed hint string it ships."
    ),
    ("completion.py", "cmd_completion"): (
        "`neo completion <shell>` prints a script this module GENERATES from its "
        "own template table. There is no read of any file or journal on the "
        "path, so there is no untrusted content to sanitise."
    ),
    ("errors.py", "explain_exception"): (
        "The crash explainer. Its values are the exception's CLASS name, the "
        "category word, the mapped `c` lines and the traceback PATH. An "
        "exception class name and a path are the two things this surface was "
        "built to show; the traceback BODY is written to a file, not printed."
    ),
    ("interactive.py", "steer_live_run"): (
        "The steering acknowledgement. `markup` is the module's own receipt "
        "string, assembled from the intent word and a turn count."
    ),
    ("interactive.py", "_reader_toggle_quiet"): (
        "The `/quiet` acknowledgement: the literal words `quiet` and `normal` "
        "selected by a boolean."
    ),
    ("interactive.py", "_reader_slash_render"): (
        "The reader thread's slash renderers. Receipts arrive from the shared "
        "command handlers and are escaped field by field here; the two values "
        "that are NOT escaped are the copy payload (a diff, which the clipboard "
        "path sanitises) and a help renderer that escapes its own rows."
    ),
    ("interactive.py", "_print_sessions"): (
        "`/sessions` index rows. Each field is `escape`d at the point it is "
        "interpolated — the task id, the repo label, the status word — and the "
        "session index stores already-redacted values."
    ),
    ("interactive.py", "_resume_conversation_command"): (
        "`/resume` receipt: a session id this module generated and the turn "
        "count it read. No journal content on the path."
    ),
    ("interactive.py", "_fork_command"): (
        "`/fork` receipt: the NEW session id the fork just minted and a turn "
        "count. The forked turns themselves are not on this path."
    ),
    ("interactive.py", "_print_feed"): (
        "`/feed` header row. The value is the module's own summary count and "
        "label; the feed ENTRIES are rendered through `tracelog`, which clips "
        "every one of them with `strip_ansi`."
    ),
    ("interactive.py", "_render_settled_approval"): (
        "The approval receipt after a gate closes. The values are the decision "
        "word and the effect summary `cli.commands.approval_request_view` "
        "already redacts — that projection is the module's declared boundary."
    ),
    ("interactive.py", "print_run_recovery"): (
        "The killed-run notice. The value is a recovery SENTENCE this module "
        "builds from the survivor report's counts."
    ),
    ("interactive.py", "_resume_task"): (
        "The `/resume` refusal and receipt rows. Values are a task id, a state "
        "word and a log root path — identifiers this product minted, not file "
        "content. The restore receipt itself is rendered by `cli.fileview`."
    ),
    ("interactive.py", "cmd_continue"): (
        "The `--continue` refusal when nothing resumable exists. The value is a "
        "searched artifact root path, built from the settings chain."
    ),
    ("interactive.py", "cmd_list_sessions"): (
        "`/sessions` for the script surface. Same escaped, index-derived fields "
        "as the TUI browser, so the two surfaces cannot disagree."
    ),
    ("interactive.py", "_resume_conversation"): (
        "The `/resume` receipt rows: the session id, the turn count and the "
        "repository path the conversation belongs to."
    ),
    ("interactive.py", "cmd_resume"): (
        "`/resume <id>` acceptance and refusal rows. Values are the id the user "
        "typed and a state word."
    ),
    ("interactive.py", "_print_session_head"): (
        "The session banner: the artifact root path and the model label, both "
        "built from the settings chain this process already resolved."
    ),
    ("interactive.py", "print_resume_briefing"): (
        "The resume briefing. Every row comes from `runview.briefing_lines`, "
        "whose producers run every journal-derived string through `strip_ansi` "
        "before it reaches a renderer."
    ),
    ("interactive.py", "_startup_recovery_offer"): (
        "The startup corrupt-session notice: a session id from the store's own "
        "filename and an error class name."
    ),
    ("interactive.py", "print_first_run"): (
        "The first-run screen, which delegates to `onboarding.render_first_run`. "
        "That renderer's producer is `cli.tui.NeoApp._print_first_run`, which "
        "clips with `strip_ansi` before the transcript."
    ),
    ("interactive.py", "run_interactive"): (
        "The REPL loop's own banner, prompt hint and quit rows. Values are the "
        "wordmark, a model label and the literal hint strings; every receipt "
        "this loop prints goes through the shared `_slash_command` handlers."
    ),
    ("interactive.py", "_decide_pending"): (
        "The pending-decision receipt: a decision KIND word and the question or "
        "permission subject, both produced by `runview.pending_decision`."
    ),
    ("interactive.py", "_slash_say_default"): (
        "The shared say hook. The value is a handler's receipt line, which the "
        "handlers build through their own escaping."
    ),
    ("interactive.py", "_slash_command_impl"): (
        "The REPL dispatcher — the largest render site set in the tree. Every "
        "branch prints either a product refusal sentence, an escaped field, or "
        "a receipt from a module that renders its own rows. The branches that "
        "carry CONTENT (a rationale, a feed entry, a session row, a diff) all "
        "escape at the interpolation or sanitise at the producer, and the "
        "durable-panels mirror of this dispatcher is what the AST parity pin in "
        "`tests/test_cli_terminal_parity.py` compares against."
    ),
    ("interactive.py", "_print_answer_head"): (
        "The answer header: a question count and the repository label this "
        "process resolved. The answer BODY is painted by the stream painters, "
        "not here."
    ),
    ("interactive.py", "_run_one_question"): (
        "The question-mode completion rows. The ANSWER body is printed by "
        "`streamview`'s painters, which clip with `strip_ansi`; the rows here "
        "are the cost and mode chips."
    ),
    ("interactive.py", "_run_one_research"): (
        "The research-mode completion rows: the same cost and mode chips as the question path, plus the source count the research renderer reports."
    ),
    ("interactive.py", "_run_one_build"): (
        "The build-mode acceptance rows: step counts and file names from the "
        "run's own journal, each escaped at the interpolation."
    ),
    ("interactive.py", "_trust_note"): (
        "The trust boundary's operator note — a sentence this module composes "
        "from the resolved `DailyTrust` receipt, not from untrusted content."
    ),
    ("interactive.py", "render_trust_banner"): (
        "The trust banner. Values come from `shared.approval.DailyTrust`, whose "
        "fields are boundary words and an audit-trail path; the banner carries "
        "no prompt, tool output or file content."
    ),
    ("interactive.py", "_agent_approve_prompt"): (
        "The REPL approval prompt: the scope menu and the yes/no line. The "
        "EFFECT being approved is rendered by the modal body, which is escaped."
    ),
    ("interactive.py", "_agent_plan_preview"): (
        "The plan-preview heading: a step count and the plan's own declared "
        "step descriptions, which this module escapes."
    ),
    ("interactive.py", "_run_one_mode"): (
        "The shared mode-dispatch completion rows: the mode name, the outcome word and the elapsed count this branch resolved."
    ),
    ("interactive.py", "_run_one_agent"): (
        "The agent-mode completion rows: attempt and model-call counts, the "
        "repository label and the rationale PATH. The rationale body is "
        "rendered by `streamview`/`ui.print_diff`, both of which sanitise."
    ),
    ("interactive.py", "_execute_task"): (
        "The fix-mode completion rows: chips, cost and elapsed. The DIFF is "
        "printed by `ui.print_diff`, which sanitises every line it draws."
    ),
    ("interactive.py", "_trace_feed_command"): (
        "The `/trace` renderer. Feed summaries and raw details are clipped by "
        "`tracelog`, which calls `strip_ansi` on every field it stores."
    ),
    ("interactive.py", "_say"): (
        "The module's say hook. The value is a receipt line a handler produced upstream, through the handler's own escaping."
    ),
    ("interactive.py", "_handle_live"): (
        "The live-run acknowledgement: a phase word and a run label this process chose from the steering intent."
    ),
    ("interactive.py", "stop"): (
        "The loop's exit rows: the literal goodbye string and a fixed saved-state note. No variable reaches either of them."
    ),
    ("main.py", "_print_result"): (
        "The human-readable `neo fix` summary. Values are status words, counts, "
        "a cost and a log path; the DIFF and the rationale are printed by "
        "`ui.print_diff` and the markdown renderer, which sanitise."
    ),
    ("main.py", "cmd_fix"): (
        "`neo fix` top-level report rows: attempt counts, verification chips, a "
        "cost and artifact paths."
    ),
    ("main.py", "cmd_run_benchmark"): (
        "`neo run-benchmark` table cells: per-task status words, elapsed times and costs, all read from the scheduler's own record."
    ),
    ("main.py", "_load_subset"): (
        "The subset loader's USAGE errors. Values are an entry index and a "
        "field name from the loader's own validation vocabulary."
    ),
    ("interactive.py", "_import_command"): (
        "`/import` rows: a resolved session id, a turn count and the file name "
        "the export came from — the argument the user typed, escaped at the "
        "interpolation. The export's CONTENT is not printed here."
    ),
    ("interactive.py", "_markup_to_plain"): (
        "The receipt re-tagging helper. It renders a captured rich console into "
        "a `StringIO` and re-emits the lines so a `[` in a plugin name is "
        "escaped exactly once; the values are the rendered segments themselves, "
        "which is a render of a render."
    ),
    ("interactive.py", "_plan_preview_watch"): (
        "The plan-preview watcher's own rows: the step count, the gate word and "
        "a fixed prompt. The plan's step DESCRIPTIONS are rendered by the modal "
        "body, which escapes them."
    ),
    ("interactive.py", "_recover_command"): (
        "`/recover` rows: a session id from the store's own filename, a turn "
        "count and the fixed remediation sentence."
    ),
    ("interactive.py", "_render_undo_result"): (
        "The `/undo` receipt. The DIFF is printed by `ui.print_diff`, which "
        "sanitises every line; the rows here are the file count, the hash and "
        "the outcome word."
    ),
    ("main.py", "_run_finding_build"): (
        "One build-mode finding's receipt: a task id, a status and a log path, "
        "handed to `_print_result`."
    ),
    ("main.py", "cmd_status"): (
        "`neo status` rows. Journal-derived strings arrive through "
        "`runview.read_run_facts`, which clips every field with `strip_ansi` "
        "before a renderer sees it, and this command is the one caller the "
        "existing sanitiser pin already names."
    ),
    ("main.py", "cmd_memory_record"): (
        "`neo memory record` receipt: the number of rows ingested and the decision-store location. No decision body is printed on this path."
    ),
    ("main.py", "cmd_memory_decisions"): (
        "`neo memory query-decisions` receipt: a decision ID, a category and a "
        "one-line summary this CLI composed, not the stored rationale body."
    ),
    ("main.py", "cmd_memory_structure"): (
        "`neo memory query-structure` receipt: file and symbol COUNTS read from the code-graph index, never a source line."
    ),
    ("main.py", "cmd_memory_ingest"): (
        "`neo memory ingest` receipt: how many rows were ingested. The rows themselves are not rendered on this path."
    ),
    ("main.py", "cmd_config"): (
        "`neo config` report rows. Every value is either a config KEY or a "
        "`neoconfig.public_value()` MASKED projection — the whole point of that "
        "function is that a config listing cannot print a credential."
    ),
    ("main.py", "cmd_profile"): (
        "`neo profile` rows: profile NAMES from the settings chain plus the active marker. No key or endpoint value is printed here."
    ),
    ("main.py", "cmd_mcp_list_tools"): (
        "`neo mcp list-tools` rows: tool names and namspaced identifiers from "
        "`cli.connectors`, which are already `safe_segment`-checked."
    ),
    ("main.py", "cmd_mcp_call"): (
        "`neo mcp call` result rows. The tool RESULT is untrusted content and is "
        "the reason this row is on the list rather than sanitised inline: the "
        "result is rendered as a receipt by `cli.connectors`, which is the "
        "module that owns the MCP trust decision."
    ),
    ("main.py", "cmd_mcp"): (
        "`neo mcp` registry rows: connector labels from the registry and `public_value`-masked launch commands, which is the projection whose whole job is masking."
    ),
    ("main.py", "cmd_mcp_permissions"): (
        "`neo mcp permissions` rows: connector labels, tool names and the "
        "declared capability words."
    ),
    ("main.py", "cmd_doctor"): (
        "`neo doctor` rows. Each check supplies its own reason and remediation "
        "strings, and the whole document is additionally passed through "
        "`redact_text` before printing — `doctor_record()` is specified as the "
        "REDACTED projection of the raw record."
    ),
    ("main.py", "cmd_support_bundle"): (
        "`neo support-bundle` receipt: the section names, per-section counts and the output path this command just wrote."
    ),
    ("main.py", "cmd_migrate"): (
        "`neo migrate` report rows: detector ids from a CLOSED vocabulary and a "
        "per-file path this command just wrote."
    ),
    ("main.py", "_print_migration_plan"): (
        "The migration PLAN receipt: the same closed detector ids, plus a "
        "target path per action. Nothing here reads a file's content."
    ),
    ("main.py", "cmd_hooks"): (
        "`neo hooks` rows: hook event names from `extensions.user_hooks` and the "
        "hook config's own command list."
    ),
    ("main.py", "cmd_skills"): (
        "`neo skills` rows: skill names, origins and description heads read from "
        "a `SKILL.md` frontmatter block. The BODY is not printed here, and a "
        "skill body is untrusted content by declaration in "
        "`shared.security.UNTRUSTED_SOURCES`."
    ),
    ("main.py", "cmd_plugin"): (
        "`neo plugin` rows: plugin names, versions and counts from a manifest "
        "this product validated at install time. Descriptions are printed by "
        "`plugin_runtime.escape_lines`."
    ),
    ("main.py", "cmd_run_command"): (
        "`neo run` receipt: the command word, a status word and an exit code drawn from the shared exit vocabulary."
    ),
    ("main.py", "_cmd_schedule_run"): (
        "The schedule-registration receipt: a schedule id (a digest the product "
        "computed), an ISO time and a command word."
    ),
    ("main.py", "cmd_dashboard"): (
        "`neo dashboard` receipt: the bound URL and port this process opened, plus the browser hint."
    ),
    ("main.py", "cmd_scan"): (
        "`neo scan` finding rows. A finding's message is scanner output over "
        "repository source; it is the strongest candidate in this file for "
        "sanitising inline and is the first item in this round's handoff list of "
        "script-surface gaps."
    ),
    ("main.py", "_run_finding"): (
        "One `neo scan` finding row: a file path, a line number and a rule "
        "name, rendered from the scanner's structured record."
    ),
    ("main.py", "cmd_analyze_history"): (
        "`neo analyze-history` rows: run ids, statuses and cost figures derived "
        "from journals this module reads through `runview`."
    ),
    ("main.py", "cmd_serve"): (
        "`neo serve` rows: the bound host, the bound port and the one-line handshake a script reads from stdout."
    ),
    ("main.py", "cmd_acp"): (
        "`neo acp` rows: the editor name from the flag and a protocol-frame count. STDOUT carries frames and nothing else."
    ),
    ("main.py", "cmd_capabilities"): (
        "`neo capabilities` rows: capability names from the RUNTIME REGISTRY "
        "and an installed-module status."
    ),
    ("main.py", "_cmd_headless_prompt"): (
        "The headless failure line: the task id this process minted and a status word from the exit vocabulary."
    ),
    ("main.py", "_emit_release_staleness_notice"): (
        "The release-staleness notice: two version strings, one from the public index and one from the installed package metadata."
    ),
    ("main.py", "_artifact_log_root"): (
        "The artifact-root warning: a repository path and the `.gitignore` "
        "entry this command just ensured."
    ),
    ("main.py", "main"): (
        "`main()`'s top-level error line: the CLASS name of an exception, so a failure is nameable without quoting its message."
    ),
    ("main.py", "render"): (
        "A `cmd_*` dispatch helper's own receipt pass-through: it prints the lines a handler returned, which the handler escaped."
    ),
    ("main.py", "_write"): (
        "The `--json` document writer's stderr note: the NAME of a stream, never its contents. stdout carries the document only."
    ),
    ("main.py", "_ready"): (
        "The readiness banner: the wordmark, a version string and a model label, all resolved from the settings chain."
    ),
    ("models.py", "cmd_models"): (
        "`neo models` picker rows: provider ids and model names read from the "
        "settings chain, escaped through the module's own `escape_lines`."
    ),
    ("onboard.py", "run_repl_wizard"): (
        "The `/login` wizard prompts: step labels and the fixed provider menu "
        "this module's own `PROVIDERS` table supplies."
    ),
    ("onboard.py", "_noninteractive_login"): (
        "The non-interactive login refusal: the provider id from the argument "
        "list and the remediation sentence."
    ),
    ("onboard.py", "cmd_login"): (
        "`neo login` result rows: a provider display name and a masked key "
        "projection from `cli.auth`, which cannot hold a literal."
    ),
    ("onboard.py", "cmd_logout"): (
        "`neo logout` receipt: the provider ids it removed, read as STORE KEYS. The masked projection carries the key, not this row."
    ),
    ("onboard.py", "missing_credentials_exit"): (
        "The missing-credentials refusal: a fixed sentence plus the names of "
        "the environment variables it looked for, never their values."
    ),
    ("selfupdate.py", "cmd_update"): (
        "`neo update` rows: version strings, a detected install method and the "
        "exact command it printed. The method detector reads a PATH, which is "
        "why this row is declared rather than assumed safe."
    ),
    ("serve.py", "acp_stdio_serve"): (
        "The `neo acp` stderr notices. STDOUT carries protocol frames and "
        "nothing else, which is the documented reason these rows exist at all."
    ),
    ("tui.py", "_copy_payload_to_clipboard"): (
        "`ctrl+y` / `/copy-diff`. The payload is a diff this run produced; it "
        "reaches the clipboard through `cli.session.copy_text_to_clipboard`, and "
        "the diff's lines were sanitised at `cli/fileview.py`'s PARSE boundary "
        "before they ever became a payload."
    ),
    ("ui.py", "print_diff"): (
        "`ui.print_diff` is the DIFF RENDERER, so its input is diff content by "
        "definition. Every line it draws has already been through "
        "`sanitize_text` — at `cli/fileview.py::_parse_hunks` on the TUI/REPL "
        "path, and inside `cli.main`'s run result on the flag path."
    ),
    ("ui.py", "print_splash"): (
        "The splash: the logo ramp, a version, a repository path and a model "
        "label. All product-owned scalars."
    ),
    ("ui.py", "print_compact_header"): (
        "The compact header: the wordmark, a version, a model label and a repository path - four product-owned scalars."
    ),
    ("uninstall.py", "cmd_uninstall"): (
        "`neo uninstall` plan rows: paths this command enumerated itself from "
        "the Neo-owned roots, never a file's content."
    ),
    ("neoconfig.py", "_warn"): (
        "The settings warn-once notice: a tier file PATH and the reason this "
        "module classified, both produced from the settings chain."
    ),
}

#: The render paths that carry UNTRUSTED CONTENT, and therefore must reach the
#: sanitiser. This is the list the prompt's T4.W2.5 asks for, expressed as a
#: gate rather than as a paragraph somebody has to trust.
#:
#: Each entry is `(module, function)` plus WHAT it carries. The gate asserts the
#: function's own body references a sanitiser, so the day somebody adds a render
#: path that bypasses the sanitiser — Phase 1's `review.py` mount, Phase 5's
#: diff pane and git UI — this goes red on the FUNCTION, before any behavioural
#: test has a chance to notice.
UNTRUSTED_RENDER_FUNCTIONS: Dict[Tuple[str, str], str] = {
    ("interactive.py", "_render_review"): "the `/review` rationale and diff body",
    (
        "interactive.py",
        "_reader_slash_render",
    ): "the reader thread's receipts and help rows",
    ("interactive.py", "_print_sessions"): "`/sessions` index rows",
    ("interactive.py", "_trace_feed_command"): "feed summaries and raw trace details",
    ("interactive.py", "_run_one_agent"): "the agent rationale path and diff handoff",
    ("interactive.py", "_run_one_build"): "build-mode acceptance rows",
    ("interactive.py", "_run_one_question"): "question-mode answer chrome",
    ("interactive.py", "_run_one_research"): "research-mode source rows",
    ("interactive.py", "_run_one_mode"): "the shared mode-dispatch rows",
    ("interactive.py", "_execute_task"): "fix-mode completion rows",
    ("interactive.py", "_slash_command_impl"): "every REPL dispatch branch",
    ("main.py", "cmd_status"): "`neo status` journal rows",
    ("tui.py", "_diff_body"): "the TUI diff modal body",
    ("tui.py", "_render_diff_value"): "the diff modal's per-file rows",
    ("tui.py", "transcript"): "every line the TUI writes to the transcript",
    ("tui.py", "_m"): "the markup role rewriter every captured segment passes",
    ("tui.py", "_styled_lines_from_segments"): "captured console segments",
    ("tui.py", "_result_facts"): "the completion card's facts",
    ("tui.py", "_note_result"): "the completion card's rows",
    ("tui.py", "_render_review"): "the TUI review screen body",
    ("tui.py", "_render_undo"): "the undo receipt rows",
    ("tui.py", "_render_header"): "the header's repository and model cells",
    ("tui.py", "_row_label"): "every rail row label the shell draws",
    ("tui.py", "_store_diagnostic"): "a stored diagnostic's message",
    ("tui.py", "_on_prompt_body"): "the approval modal's prompt body",
    ("tui.py", "_agent_approve_fn"): "the approval dialog's effect body",
    ("tui.py", "_agent_approve_kernel"): "the kernel approval dialog's body",
    (
        "tui.py",
        "_print_splash",
    ): "the TUI splash's wordmark, version and repository label",
    ("tui.py", "_print_first_run"): "the TUI first-run screen",
    ("tui.py", "_set_status"): "the status chip's text",
    ("tui_components.py", "_bound_value"): "a bounded cell's value",
    ("tui_components.py", "fit_header"): "header segment values",
    ("tui_components.py", "brand_markup"): "the wordmark ramp row",
    ("tui_components.py", "brand_floor"): "the collapsed brand floor",
    ("runview.py", "read_run_facts"): "every journal-derived fact a card renders",
    ("runview.py", "normalize_diagnostic"): "a diagnostic's message",
    ("runview.py", "_verification"): "a verification row's strings",
    ("runview.py", "_record_file_change"): "a changed-file row's path and verdict",
    ("runview.py", "_record_checkpoint_files"): "a checkpoint's file rows",
    ("runview.py", "_record_question"): "a question's subject",
    ("runview.py", "_add_file"): "a changed-file row's path and verdict",
    ("runview.py", "_apply_permission"): "a permission row's effect",
    ("tracelog.py", "_clip"): "every field of every feed entry",
    ("tracelog.py", "_on_model_response"): "a model response's text",
    ("tracelog.py", "_on_tool_result"): "a tool result's output",
    ("tracelog.py", "_on_edit_applied"): "an edit row's detail",
    ("tracelog.py", "__init__"): "the feed's constructor rows",
    ("fileview.py", "_sanitize_diff"): "the diff parse boundary itself",
    ("a11y.py", "describe_status"): "a status sentence's text",
    ("a11y.py", "_words"): "an announcement's words",
    ("headless.py", "render_human"): "the headless human renderer",
    ("command_exec.py", "run_command_line"): "the `neo run` receipt",
}


#: Render paths that carry untrusted content and do NOT yet reach the
#: sanitiser. This table is the honest other half of the pin above: naming a
#: gap is not the same as fixing one, and a pin that only names what already
#: works reads as coverage when it is not.
#:
#: Two of these rows were written by this round and then REMOVED from
#: :data:`UNTRUSTED_RENDER_FUNCTIONS` because the gate proved the claim was
#: wrong: `cli/tui.py::NeoApp.on_mount` is a widget-tree composition and
#: `cli/runview.py::_consume_unchecked` is a journal projection that never
#: prints. A gate that caught two of the gate author's own false claims is
#: evidence it measures something; the removals are recorded here rather than
#: quietly edited away.
UNTRUSTED_RENDER_GAPS: Dict[Tuple[str, str], str] = {
    ("interactive.py", "cmd_list_sessions"): (
        "REAL GAP. `/sessions` on the script surface escapes each field at the "
        "interpolation but never sanitises, while the TUI twin "
        "`interactive._print_sessions` does. The index rows carry issue text, "
        "which is untrusted by declaration. Owner: the interactive.py owner. "
        "One line: wrap the row in `ui.strip_escapes` the way the TUI twin "
        "already does, and then move the row out of this table."
    ),
    ("tui_components.py", "render"): (
        "REAL GAP. A component renderer's own body calls only `escape`, so it "
        "protects rich markup and nothing else. Its callers today pass `Text` "
        "objects, which have no markup parser and therefore no secret filter "
        "either. Owner: the tui_components owner. Closing it means the "
        "component clips its values, which is a behaviour change to whichever "
        "of its four subclasses pass a string."
    ),
    ("interactive.py", "_print_feed"): (
        "PRODUCER-BOUND, not a hole. The values are clipped by "
        "`cli.tracelog._clip`, which calls `strip_ansi` on every field the "
        "feed stores, so the sanitisation happens one layer up and this "
        "renderer relies on it. Recorded rather than omitted because a "
        "producer-bound guarantee is invisible from the renderer: a future edit "
        "that adds a field to `FeedEntry` without clipping it in `_clip` would "
        "leak through a renderer that looks correct. Owner: the tracelog owner, for "
        "`_clip`; the honest closing move is to move the clipping into the "
        "renderer so the guarantee is visible where the bytes cross."
    ),
}


# ---------------------------------------------------------------------------
# Gate 1 — every render site is classified.
# ---------------------------------------------------------------------------


#: Parsed module trees, memoised for the same reason :data:`_SITES_CACHE` is.
#: Three gates read them and each read was a full re-parse of `cli/`.
_TREES_CACHE: "Dict[str, ast.AST] | None" = None


def _module_sources() -> Dict[str, ast.AST]:
    global _TREES_CACHE
    if _TREES_CACHE is None:
        _TREES_CACHE = {
            path.name: ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for path in sorted(CLI_DIR.glob("*.py"))
            if not path.name.startswith("test_")
        }
    return _TREES_CACHE


#: One scan, memoised. The scan parses ~800 KB of `cli/` source, and five gates
#: need the result; measured at 34.6 s for the file before this cache and 8.1 s
#: after, which is the difference between a per-commit gate and a nightly one.
#: A cache in a TEST module is safe because the tree does not change underneath a
#: single pytest process, and the demonstration test below deliberately reads
#: AROUND it with an explicit path list.
_SITES_CACHE: "List[RenderSite] | None" = None


def _all_sites() -> List[RenderSite]:
    global _SITES_CACHE
    if _SITES_CACHE is None:
        _SITES_CACHE = render_sites()
    return _SITES_CACHE


def test_every_render_site_is_classified() -> None:
    """No display write in ``cli/`` may be un-classified.

    A brand-new render path in a brand-new function has no allowlist row, so it
    is red here, and the fix is a written reason or a call to the sanitiser —
    never a wider gate.
    """
    sites = _all_sites()
    assert sites, "the render-site scan found nothing; the sink vocabulary is wrong"
    unclassified = sorted(
        {site.key for site in sites if classify(site) == "declared"}
        - set(DECLARED_RENDER_FUNCTIONS)
    )
    assert not unclassified, (
        "these functions write to a display sink with an unclassified value and "
        "have no allowlist row: "
        + ", ".join(f"{module}:{fn}" for module, fn in unclassified)
    )


def test_the_scan_is_not_vacuous() -> None:
    """A gate that finds nothing proves nothing.

    Pinned floors measured on this tree, so a refactor that narrows the sink
    vocabulary (or a `cli/` restructure that renames every sink) fails here
    rather than silently passing.
    """
    sites = _all_sites()
    modules = {site.module for site in sites}
    assert len(sites) > 700, f"only {len(sites)} render sites found; the scan shrank"
    assert len(modules) >= 12, sorted(modules)
    # The surfaces the prompt names must all be scanned, or the pin does not
    # cover the paths Phase 1 and Phase 5 will add to. Two modules are
    # deliberately NOT in this list, and both absences are facts rather than
    # gaps: `cli/runview.py` and `cli/fileview.py` are PRODUCERS — they return
    # lines and sanitised hunks respectively and never write to a sink — so
    # requiring a render site there would be asserting something untrue about
    # them. Their sanitisation is pinned at the producer instead.
    for required in ("interactive.py", "main.py", "tui.py", "commands.py", "ui.py"):
        assert required in modules, f"{required} has no render site in the scan"


def test_the_allowlist_has_no_stale_rows() -> None:
    """An allowlist row for a function that no longer exists is a false claim.

    This is the shrink-only idiom: the table may only describe the tree as it is.
    Without it, the table accretes rows for deleted functions and the next
    exemption has somewhere to hide.
    """
    live = set()
    for path in sorted(CLI_DIR.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                live.add((path.name, node.name))
    stale = sorted(set(DECLARED_RENDER_FUNCTIONS) - live)
    assert not stale, (
        "these allowlist rows name functions that are not in the tree: "
        + ", ".join(f"{m}:{f}" for m, f in stale)
    )


def test_every_allowlist_row_carries_a_reason_a_reviewer_can_check() -> None:
    """A row without a reason is a hole with a comment on it.

    The length bound is not a style rule: it is the cheapest available proxy for
    "somebody wrote down what this path renders", and a one-word reason fails
    it. The reasons are also required to be sentences.
    """
    for key, reason in DECLARED_RENDER_FUNCTIONS.items():
        assert reason.strip(), key
        assert len(reason) >= 90, f"{key}: the reason is {len(reason)} chars"
        assert reason.strip().endswith((".", ")")), key


def test_no_function_is_allowlisted_unnecessarily() -> None:
    """An allowlist row for a function that needs no reason is rot.

    If a function's sites all classify as `literal`/`sanitised`/`escaped`, its
    row is dead weight — and dead weight in a gate is a place the next
    exemption goes.
    """
    needing = {site.key for site in _all_sites() if classify(site) == "declared"}
    redundant = sorted(set(DECLARED_RENDER_FUNCTIONS) - needing)
    assert not redundant, (
        "these allowlist rows cover no unclassified render site and should be "
        "deleted: " + ", ".join(f"{m}:{f}" for m, f in redundant)
    )


# ---------------------------------------------------------------------------
# Gate 3 — the untrusted render paths reach the sanitiser.
# ---------------------------------------------------------------------------


class TestTheUntrustedRenderPathsArePinned:
    """The paths that carry untrusted content must reach the sanitiser."""

    def test_every_named_path_exists(self) -> None:
        modules = _module_sources()
        missing = []
        for module, function in UNTRUSTED_RENDER_FUNCTIONS:
            tree = modules.get(module)
            if tree is None:
                missing.append(f"{module} (no such module)")
                continue
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if function not in names:
                missing.append(f"{module}:{function}")
        assert not missing, (
            "these named render paths are not in the tree: " + ", ".join(missing)
        )

    def test_every_named_path_reaches_a_sanitiser(self) -> None:
        """Each named path's OWN body references the sanitiser.

        This is the gate that catches the round that has not happened yet: a new
        render path that bypasses the sanitiser is red here, at the function,
        rather than discovered by somebody reading a pty capture.
        """
        modules = _module_sources()
        offenders: List[str] = []
        for (module, function), carries in UNTRUSTED_RENDER_FUNCTIONS.items():
            tree = modules[module]
            node = next(
                (
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == function
                ),
                None,
            )
            assert node is not None, f"{module}:{function}"
            if not _called_names(node, SANITISERS):
                offenders.append(f"{module}:{function} (carries {carries})")
        assert not offenders, (
            "these render paths carry untrusted content but never reference the "
            "sanitiser: " + "; ".join(offenders)
        )

    def test_every_named_path_says_what_it_carries(self) -> None:
        for key, carries in UNTRUSTED_RENDER_FUNCTIONS.items():
            assert carries.strip(), key
            assert len(carries) >= 20, f"{key}: the description is too short"

    def test_the_table_is_not_the_whole_tree(self) -> None:
        """The untrusted table is a CLAIM about content, not a coverage boast.

        If it ever covered every render site the reasons would stop being
        claims and start being filler, so this asserts the two sets are
        genuinely different and that the difference is explained by the
        allowlist.
        """
        needing = {site.key for site in _all_sites() if classify(site) == "declared"}
        pinned = set(UNTRUSTED_RENDER_FUNCTIONS)
        assert pinned and needing
        assert pinned != needing, (
            "the untrusted-render table and the unclassified-render table are the "
            "same set, which means one of the two is not making a claim"
        )


class TestTheUntrustedRenderGapsAreNamed:
    """The paths that do NOT reach the sanitiser, named rather than omitted.

    An invisible gap is how a broken redactor becomes a permanent condition
    nobody notices, and the same is true of a gap in the display path. These
    tests exist so the gap table cannot quietly empty: a round that fixes one of
    these has to delete its row and say so, which is a reviewable diff.
    """

    def test_every_gap_names_what_would_close_it(self) -> None:
        assert UNTRUSTED_RENDER_GAPS, (
            "the untrusted-render gap table is empty; if every gap was fixed, "
            "delete this class and say so in the handoff rather than leaving a "
            "test that can no longer fail"
        )
        for key, reason in UNTRUSTED_RENDER_GAPS.items():
            assert len(reason) >= 120, f"{key}: the reason is {len(reason)} chars"
            assert "Owner:" in reason, (
                f"{key}: a gap with no named owner is a gap nobody will close"
            )

    def test_the_gap_table_and_the_pinned_table_are_disjoint(self) -> None:
        overlap = set(UNTRUSTED_RENDER_GAPS) & set(UNTRUSTED_RENDER_FUNCTIONS)
        assert not overlap, (
            "these paths are in both tables; a path either reaches the sanitiser "
            "or it is a named gap, and being in both is a contradiction: "
            + ", ".join(f"{m}:{f}" for m, f in sorted(overlap))
        )

    def test_the_two_false_claims_stay_removed(self) -> None:
        """Two of this round's own claims were wrong, and the gate proved it.

        `NeoApp.on_mount` composes a widget tree and `runview._consume_unchecked`
        is a journal projection that never prints. Listing either as an
        untrusted render path would have made the table read as coverage it does
        not provide. The removals are pinned so a later round does not
        re-add them out of a good intention.
        """
        both = set(UNTRUSTED_RENDER_FUNCTIONS) | set(UNTRUSTED_RENDER_GAPS)
        assert ("tui.py", "on_mount") not in both
        assert ("runview.py", "_consume_unchecked") not in both

    def test_every_gap_really_does_fail_to_reach_the_sanitiser(self) -> None:
        """A gap row that has quietly been FIXED is a stale claim.

        The mirror of `test_the_allowlist_has_no_stale_rows`, and it is the
        thing that makes deleting a row the natural response to fixing a gap
        rather than leaving a table that under-reports the product.
        """
        modules = _module_sources()
        for module, function in UNTRUSTED_RENDER_GAPS:
            tree = modules[module]
            node = next(
                (
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == function
                ),
                None,
            )
            assert node is not None, f"{module}:{function} is not in the tree"
            assert not _called_names(node, SANITISERS), (
                f"{module}:{function} is listed as a gap but now references the "
                "sanitiser — the gap was fixed and its row was not deleted"
            )


# ---------------------------------------------------------------------------
# The order pin, read from the source rather than observed from the outcome.
# ---------------------------------------------------------------------------


class TestTheSanitizeOrderIsPinnedInSource:
    def test_strip_happens_before_redact_in_the_pipeline_body(self) -> None:
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "sanitize_text"
        )
        lines: Dict[str, int] = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and _callee_name(node) in {
                "strip_escapes",
                "redact_or_fail",
            }:
                lines.setdefault(_callee_name(node), node.lineno)
        assert "strip_escapes" in lines and "redact_or_fail" in lines, lines
        assert lines["strip_escapes"] < lines["redact_or_fail"], (
            f"cli/ui.py::sanitize_text redacts at line {lines['redact_or_fail']} "
            f"before stripping at line {lines['strip_escapes']}"
        )

    def test_the_historical_name_delegates_to_the_pipeline(self) -> None:
        """`strip_ansi` is ONE implementation with a second name.

        Two names for two orderings is how the ordering bug came back once
        already: one function had the right order and its alias had the wrong
        one. There is one implementation now and this pins it.
        """
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "strip_ansi"
        )
        names = {
            _callee_name(node) for node in ast.walk(fn) if isinstance(node, ast.Call)
        }
        assert names == {"sanitize_text"}, names

    def test_no_module_outside_the_sanitiser_calls_the_authority_directly(self) -> None:
        """A DISPLAY path must not reach the redactor directly for DISPLAY.

        Scoped two ways, and both scopings were found by measuring rather than
        by taste:

        * to functions that BOTH write to a display sink AND call
          ``shared.security.redact_text``, because the claim that matters is
          about the ORDER, not about the redactor. An unscoped version of this
          gate named 28 call sites across `doctor.py`, `connectors.py`,
          `onboard.py` and `neoconfig.py`, every one of which is a correct
          redaction of a value that is never printed.
        * and to calls with NO EXPLICIT-SECRET argument. `redact_text(err,
          [api_key])` scrubs a literal the process HOLDS — that is a secret
          boundary, not a display decision — and routing it through the
          sanitiser would be wrong in both directions: the sanitiser has no
          `secrets` parameter, and a value nobody can see in any redacted form
          is exactly what that call is for.

        Five real offenders were found and FIXED by this round's gate
        (`cli/main.py`'s three `cmd_mcp*` paths, `cli/onboard.py`'s two
        `/login` paths), and the order they were fixed in is the order the gate
        names them.
        """
        offenders: List[str] = []
        for path in sorted(CLI_DIR.glob("*.py")):
            if path.name == "ui.py" or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            bound = _bound_sink_receivers(tree)
            for fn in [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]:
                display_redactions = [
                    node
                    for node in ast.walk(fn)
                    if isinstance(node, ast.Call)
                    and _callee_name(node) == "redact_text"
                    and len(node.args) < 2
                    and "secrets" not in {kw.arg for kw in node.keywords}
                ]
                if not display_redactions:
                    continue
                renders = any(
                    _is_render_call(node, bound)
                    for node in ast.walk(fn)
                    if isinstance(node, ast.Call)
                )
                if renders:
                    offenders.append(
                        f"{path.name}:{fn.name}:{display_redactions[0].lineno}"
                    )
        assert not offenders, (
            "these functions redact a value on its way to a display sink and "
            "therefore skip the strip-then-redact order: "
            + ", ".join(sorted(offenders))
        )


# ---------------------------------------------------------------------------
# The demonstration. A gate nobody has watched fail is a gate nobody trusts.
# ---------------------------------------------------------------------------


def test_the_pin_fails_on_a_deliberately_raw_render_site(tmp_path) -> None:
    """Add a raw render site, watch the pin catch it, remove it.

    The probe module is written INTO `cli/` because the pin is
    directory-driven — that is the point of a directory-driven pin, and it is
    also why this test can prove the pin sees a path that did not exist when it
    was written. It is removed in a `finally`, and the pin is re-run afterwards
    so the suite cannot leave a red tree behind.
    """
    probe = CLI_DIR / "_render_pin_probe.py"
    source = (
        '"""TEMPORARY probe for the render-path pin. Deleted by the test."""\n'
        "from pathlib import Path\n\n"
        "import shared.security\n"
        "from cli import interactive, main, tui\n"
        "from cli import ui as _ui\n"
        "from cli.runview import failure_lines\n"
        "\n"
        "\n"
        "def render_untrusted_row(row):\n"
        '    """A render path that skips the sanitiser, on purpose."""\n'
        "    journal_value = row.get('detail') or ''\n"
        "    interactive.print(f'  {journal_value}')\n"
        "    return journal_value\n"
    )
    assert not probe.exists(), "a previous run of this test left its probe behind"
    global _SITES_CACHE
    cached = _SITES_CACHE
    before = {site.key for site in _all_sites() if classify(site) == "declared"}
    try:
        probe.write_text(source, encoding="utf-8")
        # The cache is dropped deliberately: the point of the probe is to prove
        # the pin sees a file that did not exist when the cache was built.
        _SITES_CACHE = None
        after = {
            site.key for site in render_sites([probe]) if classify(site) == "declared"
        }
        assert after, "the probe's raw render site was not detected at all"
        assert ("_render_pin_probe.py", "render_untrusted_row") in after, sorted(after)
        assert ("_render_pin_probe.py", "render_untrusted_row") not in before
        # And the shape of the finding a reviewer would see.
        site = next(
            s for s in render_sites([probe]) if s.function == "render_untrusted_row"
        )
        assert classify(site) == "declared", classify(site)
        assert "journal_value" in site.source, site.source
    finally:
        probe.unlink(missing_ok=True)
        _SITES_CACHE = cached

    # And the pin is clean again with the probe gone.
    remaining = {site.key for site in _all_sites() if classify(site) == "declared"}
    assert not [k for k in remaining if k[0] == "_render_pin_probe.py"], remaining
    assert not [k for k in remaining if k not in DECLARED_RENDER_FUNCTIONS]


def test_a_sanitised_render_site_is_classified_as_sanitised() -> None:
    """The positive control for the demonstration above.

    Without it, "the pin caught the raw site" could mean "the pin flags
    everything", which is a different and much less useful gate.
    """
    probe = CLI_DIR / "_render_pin_probe.py"
    source = (
        "from cli import ui as _ui\n"
        "\n"
        "\n"
        "def render_the_same_row_the_right_way(row):\n"
        "    value = _ui.sanitize_text(row.get('detail') or '')\n"
        "    print(value)\n"
        "    return value\n"
    )
    try:
        probe.write_text(source, encoding="utf-8")
        verdicts = {classify(site) for site in render_sites([probe])}
        assert "sanitised" in verdicts, verdicts
        assert "declared" not in verdicts, verdicts
    finally:
        probe.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The ledger's vocabulary is closed, which is what makes the reasons countable.
# ---------------------------------------------------------------------------


def test_the_withholding_reasons_are_a_closed_vocabulary() -> None:
    """`cli.ui.WITHHELD_REASONS` is the discriminator for two identical prefixes.

    `cli/ui.py`'s own marker and the authority's marker share the prefix
    `(detail withheld:`. If the reason is not one of ours, it came from the
    authority. That test is only sound while the vocabulary is closed, and a
    reason nobody declared would be a ledger key nobody can count.
    """
    from cli import ui as _ui

    assert _ui.WITHHELD_REASONS == (
        "encoded credential",
        "not displayable",
        "redactor unavailable",
    )
    for reason in _ui.WITHHELD_REASONS:
        marker = f"(detail withheld: {reason})"
        assert _ui._is_authority_withholding(marker) is False, marker
        assert (
            _ui._is_authority_withholding(
                "(detail withheld: redaction failed: RuntimeError)"
            )
            is True
        )
        assert _ui._is_authority_withholding("key=[REDACTED_SECRET]") is False
