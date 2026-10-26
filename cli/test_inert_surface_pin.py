"""T4.W2.1 - the INERT-SURFACE pin.

Every public function in a surface module of `cli/` must be **mounted**,
**allowlisted** with a reason, or **recorded as an unmounted surface** with a
named owner. The unmounted register is SHRINK-ONLY.

**Why this file exists.**

`cli/AGENTS.md` records nine surfaces built, unit-proven and never applied.
Wave 1 mounted seven. Wave 2's job was to prove the mounts held. **Three did
not**, and all three were invisible to every AST gate Wave 1 wrote:

| # | defect | why the AST gates passed |
|---|---|---|
| 1 | `tui.py::_review_surface` called `self._settings()`; ``NeoApp`` has no such method. Both branches raised ``AttributeError``, `except Exception: return False` turned it into "review declined", and the TUI's `/diff` rendered the **historical** diff. | the call site was textually correct - `review.review_command(` is right there in the AST |
| 2 | Both dispatchers parsed `/diff accept <file>#1` as a **filename** before asking `cli.review` about a verb, so per-hunk verdicts were unreachable from **both** shells. | both dispatchers have a `/diff` branch, and the parity pin compares dispatch **keys** |
| 3 | `interactive._render_review_surface` read the task out of `state["last"]`, which only the session loop populates; `command_exec` passes it as an **argument**. Headless got no task and declined. | the function calls `active_task_id(`, which exists |

Defect 1 is the class this file is named for: **an AST gate cannot tell
"correctly written and reachable" from "correctly written and unreachable".**
Only driving the surface can. So the mounts here are asserted three ways -
the AST shape, the settings accessor that exists, and the ORDERING (which is
the fix) - and the inert scan is what stops a tenth.

**Scope, and why it is 14 modules rather than every file in `cli/`.**
`cli/` also holds the library layer (`ui.py`, `design.py`, `tracelog.py`,
`streamview.py`) whose functions are consumed by their OWN module's other
functions. Scanning those would report hundreds of internal helpers as
"surfaces", which is a number nobody acts on. The 14 here are the modules
`cli/UNMOUNTED.md` audited - the ones the repo has already agreed are product
surfaces - so a hit means the same thing it meant there.

**The register is data, not prose.** 242 functions are named in
:data:`UNMOUNTED_SURFACES` with an owner per module. That is the honest
state of the tree (consistent with `UNMOUNTED.md`'s historical 414/494), and
it is a PIN rather than a snapshot because of four gates:

* a hit that is NOT in the register is red - new debt cannot be added quietly;
* a register row that no longer exists is red - a row cannot outlive its function;
* a register row that GAINS a call site is red - the register only shrinks,
  which is the whole difference between this and the 24 recorded handoff rows
  with 0 discharged;
* every module in the register carries a named owner.

Host-only: no Docker, no provider, no network, no credential. ``tests/**`` is
T5's, which is why this lives in ``cli/``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

CLI = Path(__file__).resolve().parent
SELF = Path(__file__).name

#: The audited surface modules - the twelve `cli/UNMOUNTED.md` named plus the
#: two whose mounts this wave proved. Test modules are excluded by
#: construction: they live beside the code and would otherwise satisfy every
#: reference the scan asks about.
SURFACE_MODULES: Tuple[str, ...] = (
    "review.py",
    "session.py",
    "command_aliases.py",
    "command_queue.py",
    "toggles.py",
    "runview.py",
    "palette.py",
    "models.py",
    "auth.py",
    "plugin_runtime.py",
    "onboarding.py",
    "fileview.py",
    "a11y.py",
    "command_types.py",
)

_TREE_CACHE: Dict[str, ast.Module] = {}
_MODULE_NAMES: Dict[str, Set[str]] = {}


def _parse(name: str) -> ast.Module:
    if name not in _TREE_CACHE:
        _TREE_CACHE[name] = ast.parse(
            (CLI / name).read_text(encoding="utf-8", errors="replace")
        )
    return _TREE_CACHE[name]


def _per_module_names() -> Dict[str, Set[str]]:
    """Every identifier, attribute and imported symbol, per module.

    One pass per module for the whole pytest process. The first two versions
    of this scan walked every AST node for every candidate - O(functions x
    nodes) - and one run took over eight minutes, which is not a gate anybody
    runs. The tree does not change underneath a pytest process.
    """
    if _MODULE_NAMES:
        return _MODULE_NAMES
    for path in sorted(CLI.glob("*.py")):
        if not path.is_file():
            continue
        if path.name.startswith(("test_", "_")) or path.name == SELF:
            # Test modules would satisfy every reference the scan asks about,
            # which is the exact "reachable FROM A TEST and nothing else" case
            # `cli/UNMOUNTED.md` names as the failure mode.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        found: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    found.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        _MODULE_NAMES[path.name] = found
    return _MODULE_NAMES


def referenced_outside(module: str, func: str) -> bool:
    """Is ``func`` named by any OTHER scanned module?

    One hop of name resolution, deliberately. A transitive "does this reach a
    surface" closure was measured during this round's design and classified
    256 functions as mounted, including a 2,000-line function with one
    ``strip_ansi`` call somewhere in its body - a closure that wide measures
    nothing. And a scan that calls ``Foo.bar`` mounted because ``bar`` appears
    in a comment is worse than no scan, so this answers the narrow question
    and the mount table answers the real one.
    """
    return any(
        func in names for name, names in _per_module_names().items() if name != module
    )


def public_functions(module: str) -> Dict[str, ast.FunctionDef]:
    """Top-level public functions.

    Deliberately top-level: a method is reached through the object that owns
    it, and ``NeoApp._review_surface`` is exactly the shape a name scan gets
    wrong. Methods are audited by :data:`MOUNT_SITES`, which names them with
    their owner.
    """
    return {
        node.name: node
        for node in _parse(module).body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }


def _function_source(module: str, name: str) -> Optional[str]:
    for node in _parse(module).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    return None


def _method_source(module: str, owner: str, method: str) -> Optional[str]:
    for node in _parse(module).body:
        if isinstance(node, ast.ClassDef) and node.name == owner:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method:
                    return ast.unparse(child)
    return None


def _region(module: str, name: str) -> str:
    found = _function_source(module, name)
    assert found is not None, f"{module}::{name} is gone"
    return found


def _method_body(module: str, owner: str, method: str) -> str:
    """A method's BODY, with its docstring removed.

    Same reason as `_body_region`: this file states its assertions as the
    failures they prevent, so a docstring that says "the mount used to call
    self._settings()" is itself a match for the assertion that forbids it.
    """
    for node in _parse(module).body:
        if isinstance(node, ast.ClassDef) and node.name == owner:
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == method:
                    docstring = ast.get_docstring(child, clean=False)
                    body = child.body[1:] if docstring else child.body
                    return ast.unparse(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"{module}::{owner}.{method} is gone")


def _body_region(module: str, name: str) -> str:
    """The function's BODY, with its docstring removed.

    A gate that greps the unparsed source matches its own PROSE: this file's
    assertions are stated as the failures they prevent, so `state.get("last")`
    appears in the docstring of the very function asserting it is gone, and
    the first version of this test failed on its own explanation. Reading the
    AST body is the fix; a comment in the module would still not satisfy it.
    """
    node = _function_source_node(module, name)
    assert node is not None, f"{module}::{name} is gone"
    docstring = ast.get_docstring(node, clean=False)
    body = node.body[1:] if docstring else node.body
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def _function_source_node(module: str, name: str) -> Optional[ast.FunctionDef]:
    for node in _parse(module).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------------------
# MOUNT_SITES - what this round PROVED by driving the surface.
# ---------------------------------------------------------------------------

MOUNT_SITES: Tuple[Tuple[str, str, str, str], ...] = (
    (
        "1.1 review surface",
        "tui.py",
        "NeoApp._review_surface",
        "the TUI's /diff mounts cli.review; both shells render the same task id, "
        "the same file list and the same fail-closed verdict",
    ),
    (
        "1.1 review surface (REPL twin)",
        "interactive.py",
        "_render_review_surface",
        "the REPL's /diff mounts cli.review and prints the same lines; a reader "
        "who cannot decline decides differently from one who can",
    ),
    (
        "1.1b per-hunk verdict routing",
        "interactive.py",
        "_review_diff_verb",
        "the REPL asks cli.review about a VERB before parsing an argument as a path",
    ),
    (
        "2.1a multi-instance guard",
        "tui.py",
        "NeoApp._acquire_instance_guard",
        "a SECOND OS PROCESS on one repository is refused; an in-process lease "
        "is re-entrant and proves only that the same pid is recognised",
    ),
    (
        "2.1b session pulse",
        "tui.py",
        "NeoApp._session_pulse",
        "the rail publishes a session section built from cli.session.session_pulse",
    ),
    (
        "2.1b session pulse (REPL twin)",
        "interactive.py",
        "_print_session_pulse",
        "/status prints the pulse, and prints nothing at all when it cannot be read",
    ),
    (
        "1.4b command queue",
        "tui.py",
        "NeoApp.on_mount",
        "the TUI's queue IS a CommandQueue and _queue writes through to it, so a "
        "caller that appended believes something real happened",
    ),
    (
        "W1.3 tri-state toggle",
        "tui.py",
        "NeoApp.action_toggle_sidebar",
        "six presses cycle auto -> show -> hide -> auto without stalling",
    ),
    (
        "W2 the session index is a listing source",
        "interactive.py",
        "list_sessions",
        "a COMPLETED run in the global index is listed, so a fresh process can "
        "resolve a task and reach every surface below it",
    ),
)


class TestEveryMountThisRoundClaimedIsStillMounted:
    def test_every_named_mount_site_still_exists(self) -> None:
        """A mount table naming a deleted function is a stale claim.

        Cheap, and it is what stops this file decaying into prose.
        """
        for label, module, func, _why in MOUNT_SITES:
            found = (
                _method_source(module, func.split(".")[0], func.split(".")[-1])
                if "." in func
                else _function_source(module, func)
            )
            assert found is not None, (
                f"{label} claims a mount at {module}::{func}, which is no "
                "longer there; the mount table is stale"
            )

    def test_the_review_mount_reads_the_config_that_ACTUALLY_EXISTS(self) -> None:
        """The regression, pinned at its own cause.

        `NeoApp` has no `_settings()`. Its config is `self.file_config`. The
        mount called the former, the `except` turned the AttributeError into
        a decline, and `/diff` rendered the historical diff while every AST
        gate passed. Asserting the NAME is what stops it coming back.
        """
        mount = _method_body("tui.py", "NeoApp", "_review_surface") or ""
        assert "self._settings()" not in mount, (
            "the review mount calls self._settings() again; NeoApp has no such "
            "method, so /diff silently declines to the historical diff"
        )
        assert "_review_config()" in mount, (
            "the review mount must read its config through _review_config()"
        )
        accessor = _method_body("tui.py", "NeoApp", "_review_config") or ""
        assert "file_config" in accessor, (
            "_review_config must read self.file_config - that is NeoApp's config"
        )

    def test_a_broken_review_backend_is_no_longer_SILENT(self) -> None:
        """Declining is legitimate; declining SILENTLY is what hid the defect.

        `review_command` returns `handled=False` for every verb it does not
        own, so a decline is a normal answer and must not be an error. An
        EXCEPTION, though, has to say so on screen or the next broken call
        site is invisible for another wave.

        Both the helper AND its CALL SITES are asserted, and that is a
        measured correction rather than thoroughness: with only the helper
        asserted, this round's own sensitivity probe replaced the mount's
        `return self._review_unavailable(exc)` with a bare `return False`
        and the gate stayed GREEN, because the helper still existed. A gate
        that passes while the behaviour it describes is gone is the exact
        class this file was written to stop, so the CALL is what is pinned.
        """
        mount = _method_body("tui.py", "NeoApp", "_review_surface") or ""
        found = mount.count("self._review_unavailable(")
        assert found >= 2, (
            "the review mount must hand BOTH its exception paths to "
            "_review_unavailable - the verb branch and the build branch. "
            f"Found {found} call(s); a mount that declines silently on one "
            "path is the defect this gate exists for."
        )
        helper = _method_body("tui.py", "NeoApp", "_review_unavailable") or ""
        assert "transcript(" in helper, (
            "_review_unavailable must PRINT the reason; returning False alone "
            "is the silent decline this file exists to prevent"
        )

    def test_both_shells_ask_the_VERB_before_parsing_a_path(self) -> None:
        """Per-hunk verdicts were unreachable because a path won the race.

        The check is POSITIONAL, because the ordering is the whole fix: both
        dispatchers used to reach the per-file branch - which treats any
        first word as a filename - before the review mount, so
        `/diff accept <file>#1` answered "no diff recorded for accept
        <file>#1", which reads exactly like a missing diff and is not one.
        """
        tui = _method_body("tui.py", "NeoApp", "_slash_command_impl") or ""
        assert tui.index("_split_review_verb") < tui.index("parse_diff_target"), (
            "the TUI parses a /diff argument as a path before asking "
            "cli.review about a verb"
        )
        repl = _body_region("interactive.py", "_slash_command_impl")
        assert repl.index("_review_diff_verb") < repl.index("parse_diff_target"), (
            "the REPL parses a /diff argument as a path before asking "
            "cli.review about a verb"
        )

    def test_the_repl_twin_reads_the_task_it_was_GIVEN(self) -> None:
        """`state["last"]` is populated by the session loop only.

        `command_exec.run_command_line` passes the resolved task as an
        ARGUMENT, so the twin got no task id and declined - which is how the
        review surface stayed unreachable headless while the same function
        was proven working in a REPL session.
        """
        source = _body_region("interactive.py", "_render_review_surface")
        # `ast.unparse` renders a string literal with SINGLE quotes, so an
        # assertion written as `state.get("last")` can never match the AST
        # output and passes whatever the tree does. That is a gate that
        # cannot fail, and this round's sensitivity probe found it by
        # mutating the very line it claims to protect and watching it stay
        # green. The check is on the RESOLUTION - which object the task id
        # comes from - not on a spelling of it.
        assert "active_task_id(Path(log_root), last" in source, (
            "_render_review_surface must resolve the task from the `last` the "
            "caller passed, not from state - two answers to 'which run is "
            "this' in one function is the defect. (The `state.get('repo')` and "
            "`state.get('config')` reads further down are legitimate and are "
            "not what this forbids; the TASK is the thing with two answers.)"
        )
        signature = _function_source("interactive.py", "_render_review_surface") or ""
        assert "last:" in signature, (
            "_render_review_surface must take `last` as a keyword the caller "
            "already resolved"
        )

    def test_the_session_index_is_a_listing_source(self) -> None:
        """A completed run in the index must be LISTED, not only resumable.

        Without this, `command_exec` resolves no task in a fresh process and
        every headless `/diff` answers "no diff from the last run" - which is
        what made the third surface fail for a different reason than the
        first two. Parity across three surfaces is the acceptance; two is not.
        """
        source = _body_region("interactive.py", "list_sessions")
        assert "index_records" in source, (
            "list_sessions does not consult the global session index, so a "
            "completed run is invisible to the headless surface"
        )


class TestTheInertScanIsSound:
    def test_the_scan_reads_the_modules_it_claims_to(self) -> None:
        """A gate over zero files is a gate that cannot fail.

        Recorded because this repo's own history has several such cases: a
        broken `_method_source` would have made every mount assertion above
        vacuous, and a wrong module list would have made the whole scan
        vacuous. The floors are measured, not asserted.
        """
        missing = [name for name in SURFACE_MODULES if not (CLI / name).is_file()]
        assert not missing, f"these scanned modules do not exist: {missing}"
        found = sum(len(public_functions(name)) for name in SURFACE_MODULES)
        assert found > 400, (
            f"the scan found only {found} public functions across "
            f"{len(SURFACE_MODULES)} modules; a number that small means the "
            "scan is not reading what it claims to"
        )

    def test_a_name_the_scan_indexes_is_actually_indexed(self) -> None:
        """The index is the fast path, so a broken one is a silent green.

        `index_records` is known to be referenced from `interactive.py`,
        `session.py` and `tui.py`. If the per-module name index ever stopped
        collecting `Attribute` nodes, every reference would look absent and
        all 242 rows would keep passing while the scan measured nothing.
        """
        index = _per_module_names()
        for module, func, _why in (
            ("interactive.py", "index_records", "the headless listing source"),
            ("review.py", "build_review", "the review document builder"),
            ("command_aliases.py", "resolve_line", "the alias seam"),
        ):
            elsewhere = any(
                func in names for name, names in index.items() if name != module
            )
            assert elsewhere, (
                f"the name index does not see {module}::{func}, which IS "
                "referenced elsewhere; the scan's fast path is broken and every "
                "inert row would keep passing for the wrong reason"
            )


class TestNoSurfaceIsSilentlyInert:
    """THE PIN. A new unclassified hit is red, and the register only shrinks."""

    def test_every_unreachable_function_is_in_the_register(self) -> None:
        """New debt cannot be added quietly.

        A hit that is neither mounted, nor allowlisted, nor recorded as an
        unmounted surface is a surface somebody built and nobody applied -
        which is the whole class this file exists for.
        """
        unrecorded = self._unregistered()
        assert not unrecorded, (
            "these public functions are defined and referenced by NO other "
            "cli/ module - a built, unapplied surface:\n"
            + "\n".join(f"  {m}::{f}" for m, f in unrecorded)
            + "\nMount it, allowlist it with a reason, or record it in "
            "UNMOUNTED_SURFACES with an owner."
        )

    def test_the_register_is_shrink_only(self) -> None:
        """A register that only GROWS is a register nobody is discharging.

        This is the exact failure `cli/UNMOUNTED.md` records for 24 recorded
        handoff rows with 0 discharged. A row that gains a call site must be
        deleted in the same change that mounted it, and this test is what
        makes that true rather than intended.
        """
        stale = [
            f"{module}::{func}"
            for module, func in UNMOUNTED_SURFACES
            if referenced_outside(module, func)
        ]
        assert not stale, (
            "these registered-unmounted rows now have a real call site. Delete "
            "each row in the same change that mounted it - a register that only "
            "grows is the debt this file was written to stop:\n"
            + "\n".join(f"  {name}" for name in stale)
        )

    def test_no_row_outlives_its_function(self) -> None:
        """A row for a deleted function is a row nobody will ever discharge."""
        dead = [
            f"{module}::{func}"
            for module, func in UNMOUNTED_SURFACES
            if func not in public_functions(module)
        ]
        assert not dead, (
            "these registered-unmounted rows name functions that no longer "
            "exist; delete them: " + ", ".join(dead)
        )

    def test_every_registered_module_has_a_named_owner(self) -> None:
        """A debt with no owner is a note, and notes do not get closed."""
        for module, owner in REGISTER_OWNERS.items():
            assert len(owner) >= 12, (
                f"{module} has no usable owner: {owner!r}. An owner is what "
                "makes a row closeable by somebody rather than by nobody."
            )
        covered = {module for module, _func in UNMOUNTED_SURFACES}
        uncovered = covered - set(REGISTER_OWNERS)
        assert not uncovered, (
            f"these modules carry inert-surface rows with no owner: {sorted(uncovered)}"
        )

    def test_the_allowlist_exempts_nothing_reachable(self) -> None:
        """An allowlist row covering a REACHED function is a hole.

        The allowlist is for names that read like surfaces and are not - a
        closed vocabulary, a constant, a screen class. A row that quietly
        covers a real mount is exactly how this class of defect returns.
        """
        stale = [
            f"{module}::{name}"
            for (module, name) in ALLOWLIST
            if referenced_outside(module, name)
        ]
        assert not stale, (
            "these allowlisted names ARE referenced from a real call site, so "
            "the exemption no longer describes anything: " + ", ".join(stale)
        )

    def test_every_allowlist_reason_is_a_reason(self) -> None:
        """A reason under 90 characters is a note, and a note is not reviewed."""
        for (module, name), reason in ALLOWLIST.items():
            assert len(reason) >= 90, (
                f"the allowlist reason for {module}::{name} is {len(reason)} "
                "characters; write one that names what the name IS"
            )
            assert name in (CLI / module).read_text(encoding="utf-8"), (
                f"allowlisted {module}::{name} no longer exists in the module"
            )

    @staticmethod
    def _unregistered() -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for module in SURFACE_MODULES:
            for func in sorted(public_functions(module)):
                if (module, func) in ALLOWLIST:
                    continue
                if (module, func) in UNMOUNTED_SURFACES:
                    continue
                if referenced_outside(module, func):
                    continue
                out.append((module, func))
        return out


#: Names that are NOT surfaces even though they look like one. Each row
#: states what the name IS, because an allowlist nobody can question is a
#: place the next unmounted surface hides.
ALLOWLIST: Dict[Tuple[str, str], str] = {
    ("review.py", "DIFF_REVIEW_VERBS"): (
        "the CLOSED review-verb vocabulary both shells read to decide whether "
        "to ask cli.review at all; it is consumed by membership tests and by "
        "the command registry's verb projection, so it is read rather than "
        "called, and no call site can exist for it."
    ),
}


#: Who would close each module's inert rows. Without an owner a row is a
#: note, and `cli/UNMOUNTED.md` is the record of what 24 unowned notes cost.
REGISTER_OWNERS: Dict[str, str] = {
    "a11y.py": "the accessibility half; the pagination block and three announce transitions",
    "auth.py": "the credential flow; connect_interactive is the mounted half",
    "command_aliases.py": "the alias table; only resolve_line is reached",
    "command_queue.py": "the queue authority; only the display helpers are read",
    "command_types.py": "the command-type vocabulary; cost_projection is dark by design",
    "fileview.py": "the file layer; the staged-undo verbs sit dark beside a mounted dispatcher",
    "models.py": "the model picker; unmounted because main.py is another owner's line",
    "onboarding.py": "the empty states and first run; task-index helpers are read via others",
    "palette.py": "the / menu; unmounted because the live ctrl+p palette is a second source",
    "plugin_runtime.py": "the plugin runtime; run_plugin_verb is dark behind a hand-rolled twin",
    "review.py": "the review surface; mounted this wave, the tail is deeper read helpers",
    "runview.py": "the run-view projection; many renderers are consumed by their own module",
    "session.py": "the session layer; the durability and index helpers are read by session.py",
    "toggles.py": "the toggle registry; the mount is action_toggle_sidebar in the TUI",
}


#: EVERY public function in a scanned module that NO other `cli/` module
#: names, with its module's owner in :data:`REGISTER_OWNERS` above.
#:
#: This is not a wish list - it is the measured state of the tree, and it is
#: consistent with `cli/UNMOUNTED.md`'s historical finding that 414 of 494
#: public symbols across twelve rounds were unmounted. Wave 2 did not grow
#: it and did not shrink it; it made it SHRINK-ONLY, which is the change that
#: stops it growing again.
#:
#: The rows are sorted so a diff of this block shows exactly one mount, and
#: :meth:`TestNoSurfaceIsSilentlyInert.test_the_register_is_shrink_only` fails
#: the moment one of them gains a call site.
UNMOUNTED_SURFACES: Dict[Tuple[str, str], str] = {
    ("review.py", "resolve_diff_style"),
    ("review.py", "tool_evidence"),
    ("review.py", "snapshot_health"),
    ("review.py", "review_text_lines"),
    ("review.py", "read_review_receipt"),
    ("review.py", "hunk_decisions"),
    ("review.py", "record_decision"),
    ("review.py", "accept_paths"),
    ("review.py", "reject_paths"),
    ("review.py", "accept_hunks"),
    ("review.py", "reject_hunks"),
    ("review.py", "revert_paths"),
    ("review.py", "changed_files_indicator"),
    ("review.py", "render_receipt"),
    ("session.py", "workspace_identity"),
    ("session.py", "event_log_path"),
    ("session.py", "new_session_id"),
    ("session.py", "conversations_dir"),
    ("session.py", "recover_session"),
    ("session.py", "resume_session"),
    ("session.py", "append_session_event"),
    ("session.py", "link_turn_to_run"),
    ("session.py", "set_active_run"),
    ("session.py", "set_model_profile"),
    ("session.py", "set_unresolved_questions"),
    ("session.py", "set_workspace_mode"),
    ("session.py", "retrieve_session_turns"),
    ("session.py", "reconstruct_session"),
    ("session.py", "export_session_document"),
    ("session.py", "promote_session_decision"),
    ("session.py", "create_checkpoint"),
    ("session.py", "checkpoint_before_mutation"),
    ("session.py", "load_checkpoint"),
    ("session.py", "diff_checkpoint"),
    ("session.py", "index_dir"),
    ("session.py", "index_session"),
    ("session.py", "index_conversation"),
    ("session.py", "index_compact"),
    ("session.py", "index_stats"),
    ("session.py", "index_roots"),
    ("session.py", "cross_repo_sessions"),
    ("session.py", "index_remove"),
    ("session.py", "session_pulse_lines"),
    ("session.py", "transcript_segments"),
    ("session.py", "transcript_segment_lines"),
    ("session.py", "session_survival_report"),
    ("session.py", "session_survival_lines"),
    ("command_aliases.py", "deferred_alias_promotions"),
    ("command_aliases.py", "alias_target"),
    ("command_aliases.py", "resolve_alias_names"),
    ("command_aliases.py", "alias_table_rows"),
    ("command_aliases.py", "alias_cycle_report"),
    ("command_aliases.py", "validate_alias_table"),
    ("command_aliases.py", "non_stackable_reason"),
    ("command_aliases.py", "expand_command_line"),
    ("command_aliases.py", "expand_command_lines"),
    ("command_aliases.py", "alias_disclosure_lines"),
    ("command_aliases.py", "alias_disclosure_text"),
    ("command_aliases.py", "safe_lines"),
    ("command_queue.py", "exempt_commands"),
    ("command_queue.py", "is_exempt"),
    ("command_queue.py", "resolve_queue_config"),
    ("command_queue.py", "queue_depth"),
    ("command_queue.py", "queue_lines"),
    ("command_queue.py", "safe_lines"),
    ("toggles.py", "toggle_mounts"),
    ("toggles.py", "toggle_spec"),
    ("toggles.py", "default_toggles"),
    ("toggles.py", "repo_toggle_path"),
    ("toggles.py", "session_toggle_path"),
    ("toggles.py", "toggles_from_config"),
    ("toggles.py", "toggle_hint_rows"),
    ("toggles.py", "render_hint_markup_rows"),
    ("runview.py", "normalize_projection_mode"),
    ("runview.py", "status_is_success"),
    ("runview.py", "verdict_is_success"),
    ("runview.py", "retrieval_projection"),
    ("runview.py", "retrieval_lines"),
    ("runview.py", "read_agent_projection"),
    ("runview.py", "read_context_receipt"),
    ("runview.py", "read_context_meter"),
    ("runview.py", "read_rewind_targets"),
    ("runview.py", "context_meter_line"),
    ("runview.py", "card_lines"),
    ("runview.py", "headless_status"),
    ("runview.py", "failure_record"),
    ("runview.py", "recovery_actions"),
    ("runview.py", "pending_decision"),
    ("runview.py", "undo_receipt"),
    # P3b/W2 T4 (this round): the event-vocabulary completion gate. Its ONLY
    # caller is its own pin, which is what a gate IS - `cache_cost` and
    # `permission_ui` ship the same shape and would land here too if their
    # modules were in the audited set. It is deliberately NOT privatised: a
    # gate is this package's published contract, and an underscore would hide
    # the contract along with the name. This row is shrink-only, so the day a
    # surface calls the gate the row goes red and must be deleted.
    ("runview.py", "assert_event_dispositions_are_complete"),
    ("palette.py", "group_for_command"),
    ("palette.py", "command_group"),
    ("palette.py", "unassigned_commands"),
    ("palette.py", "phrasings_warm"),
    ("palette.py", "mcp_entries"),
    ("palette.py", "plugin_entries"),
    ("palette.py", "palette_entries"),
    ("palette.py", "group_entries"),
    ("palette.py", "search_entries"),
    ("palette.py", "text_lines"),
    ("palette.py", "entry_row"),
    ("palette.py", "entry_markup_row"),
    ("palette.py", "entry_text_row"),
    ("palette.py", "palette_lines"),
    ("palette.py", "palette_text_lines"),
    ("palette.py", "palette_receipt"),
    ("palette.py", "slash_hook"),
    ("palette.py", "open_rows"),
    ("models.py", "picker_widget_id"),
    ("models.py", "model_catalog"),
    ("models.py", "effort_variant"),
    ("models.py", "cycle_effort"),
    ("models.py", "carry_effort"),
    ("models.py", "model_id_verdict"),
    ("models.py", "select_model"),
    ("models.py", "discover_models"),
    ("models.py", "cmd_models"),
    ("models.py", "register_models_parser"),
    ("models.py", "safe_lines"),
    ("auth.py", "provider_list"),
    ("auth.py", "provider_prompt"),
    ("auth.py", "provider_by_id"),
    ("auth.py", "needs_method_choice"),
    ("auth.py", "method_prompt"),
    ("auth.py", "provider_id_for_endpoint"),
    ("auth.py", "is_env_ref"),
    ("auth.py", "secret_source"),
    ("auth.py", "resolve_secret"),
    ("auth.py", "mask_key"),
    ("auth.py", "auth_path"),
    ("auth.py", "normalise_provider_id"),
    ("auth.py", "load_credentials"),
    ("auth.py", "save_credential"),
    ("auth.py", "remove_credential"),
    ("auth.py", "list_credentials"),
    ("auth.py", "active_credential"),
    ("auth.py", "set_active"),
    ("auth.py", "looks_like_library_leak"),
    ("auth.py", "verify_credential"),
    ("auth.py", "connect"),
    ("auth.py", "section_lines"),
    ("auth.py", "render_provider_menu"),
    ("auth.py", "render_method_prompt"),
    ("auth.py", "render_verification"),
    ("auth.py", "render_connect_result"),
    ("auth.py", "render_status"),
    ("auth.py", "status_line"),
    ("auth.py", "render_list"),
    ("auth.py", "connect_interactive"),
    ("auth.py", "cmd_connect"),
    ("auth.py", "cmd_auth_list"),
    ("auth.py", "cmd_auth_logout"),
    ("plugin_runtime.py", "plugin_data_dir"),
    ("plugin_runtime.py", "expand_path_tokens"),
    ("plugin_runtime.py", "discover_components"),
    ("plugin_runtime.py", "validate_layout"),
    ("plugin_runtime.py", "validate_source"),
    ("plugin_runtime.py", "parse_namespace"),
    ("plugin_runtime.py", "namespace_verb_rows"),
    ("plugin_runtime.py", "namespaced_lookup"),
    ("plugin_runtime.py", "scoped_root"),
    ("plugin_runtime.py", "load_scoped_plugins"),
    ("plugin_runtime.py", "installed_plugin_names"),
    ("plugin_runtime.py", "verify_installed"),
    ("plugin_runtime.py", "plugin_dependencies"),
    ("plugin_runtime.py", "semver_satisfies"),
    ("plugin_runtime.py", "dependency_report"),
    ("plugin_runtime.py", "dependents_of"),
    ("plugin_runtime.py", "enable_plugin"),
    ("plugin_runtime.py", "disable_plugin"),
    ("plugin_runtime.py", "trust_report"),
    ("plugin_runtime.py", "safe_lines"),
    ("plugin_runtime.py", "trust_decision_path"),
    ("plugin_runtime.py", "trust_decision"),
    ("plugin_runtime.py", "record_trust_decision"),
    ("plugin_runtime.py", "require_trust"),
    ("plugin_runtime.py", "projected_session_cost"),
    ("plugin_runtime.py", "read_marketplace"),
    ("plugin_runtime.py", "marketplace_at"),
    ("plugin_runtime.py", "add_marketplace_source"),
    ("plugin_runtime.py", "fetch_marketplace_document"),
    ("plugin_runtime.py", "resolve_entry"),
    ("plugin_runtime.py", "list_marketplaces"),
    ("plugin_runtime.py", "uninstall_plugin"),
    ("plugin_runtime.py", "menu_lines"),
    ("plugin_runtime.py", "run_plugin_verb"),
    ("onboarding.py", "resolve_keys"),
    ("onboarding.py", "task_index"),
    ("onboarding.py", "task_lines"),
    ("onboarding.py", "task_help_lines"),
    ("onboarding.py", "next_action"),
    ("onboarding.py", "task_phrasing_index"),
    ("onboarding.py", "onboarding_wiring"),
    ("fileview.py", "file_change_verified"),
    ("fileview.py", "read_repository_files"),
    ("fileview.py", "read_git_status"),
    ("fileview.py", "build_file_tree"),
    ("fileview.py", "symbol_candidates"),
    ("fileview.py", "current_diff_text"),
    ("fileview.py", "diff_summary"),
    ("fileview.py", "diagnostic_provenance"),
    ("fileview.py", "diagnostic_link"),
    ("fileview.py", "diagnostic_rows"),
    ("fileview.py", "undo_scopes"),
    ("fileview.py", "undo_scope_words"),
    ("fileview.py", "undo_live_refusal"),
    ("fileview.py", "staged_undo_state"),
    ("fileview.py", "stage_undo"),
    ("fileview.py", "undo_plan"),
    ("fileview.py", "commit_undo"),
    ("fileview.py", "discard_undo"),
    ("fileview.py", "undo_turn_rows"),
    ("fileview.py", "undo_scope_for"),
    ("fileview.py", "restore_selection_preflight"),
    ("fileview.py", "launch_editor"),
    ("fileview.py", "review_worktree"),
    ("fileview.py", "review_scope"),
    ("fileview.py", "settings_effective_diff"),
    ("fileview.py", "clear_file_caches"),
    ("a11y.py", "announce_idle"),
    ("a11y.py", "announce_steered"),
    ("a11y.py", "announce_paged"),
    ("a11y.py", "glyph_text"),
    ("a11y.py", "describe_status"),
    ("a11y.py", "render_code"),
    ("a11y.py", "diagnostic_glyph"),
    ("a11y.py", "diagnostic_provenance_text"),
    ("a11y.py", "paginate"),
    ("a11y.py", "page_lines"),
    ("a11y.py", "pager_body"),
    ("a11y.py", "omitted_note"),
    ("a11y.py", "describe_pagination"),
    ("a11y.py", "long_output_policy"),
    ("command_types.py", "may_fork_subagent"),
    ("command_types.py", "background_policy"),
    ("command_types.py", "subagent_agent"),
    ("command_types.py", "type_label"),
    ("command_types.py", "type_markers"),
    ("command_types.py", "type_rows"),
    ("command_types.py", "cost_projection"),
    ("command_types.py", "context_budget_names"),
    ("command_types.py", "migration_report_rows"),
    ("command_types.py", "migration_report_document"),
}
