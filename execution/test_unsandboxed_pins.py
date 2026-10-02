"""T2.W2.3 — "never run unsandboxed silently", pinned repo-wide.

**This module contains ONE test that is RED BY DESIGN.** See
:func:`test_RED_BY_DESIGN_no_production_import_path_reaches_the_local_subprocess_sandbox_stub`.
It is supposed to fail today. It is registered with T5 as known-failing and it
**must not be "fixed" by weakening it.**

The rest of the module is the stub-reachability audit, and it is green.

The cross-module hazard this pins
---------------------------------

``harness/agent_loop.py`` around lines 787-795 **imports the local-subprocess
stub and runs bash on the live host unconditionally** for the ``legacy_agent``
strategy. ``harness/AGENTS.md`` records this as *"the one place the containment
fix did not reach."* It is the single most severe trust defect in the repo: a
sandbox whose boundary has a hole in it.

``execution/`` cannot fix it — ``harness/`` is T1's file. It CAN pin it, and
that is the point: a pin is what makes Phase 2's deletion of that path a
*provable* improvement rather than an assertion in a handoff note. When T1
deletes the unsandboxed path, the inverted pin below turns green and nothing
else in this repository has to be re-audited.

The behaviour that IS correct, and is pinned here as green
------------------------------------------------------------

``execution.sandbox.execute_sandboxed`` raises ``SandboxUnavailableError`` when
the daemon is unreachable. It NEVER silently runs the command on the host.
That is the documented contract and this module proves it three ways: the
raise itself, an argv-level pin that the module's only spawned program is the
``docker`` CLI, and a closed enumeration of every import path that can reach
the local-subprocess stub.

Run: ``python -m pytest execution/test_unsandboxed_pins.py -q``
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from execution.sandbox import SandboxUnavailableError, execute_sandboxed

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent

#: Modules whose PRODUCTION source is scanned for stub reachability. Test
#: modules are scanned separately: this file imports the stub deliberately, in
#: order to assert what it is, and an auditor that flagged itself would be
#: reporting its own instrument as a defect.
PACKAGED_MODULES: Tuple[str, ...] = (
    "execution",
    "harness",
    "runtime",
    "shared",
    "memory",
    "evals",
    "cli",
    "mcp_server",
    "dashboard",
    "scripts",
)

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "logs",
        "graphify-out",
        "Temp",
        ".git",
        ".harness",
        "probe_logs",
        "site",
        "node_modules",
        ".qwen",
        ".shots",
        ".playwright-mcp",
        ".docker",
        "phases",
        "recipes",
        "repo",
        "integrations",
        "acp",
        "agent_sdk",
        "extensions",
        "fixtures",
    }
)


def _python_files(module: str, *, include_tests: bool) -> List[Path]:
    """Return ``.py`` files under a packaged module, skipping artefact trees.

    ``include_tests=False`` drops every ``test_*.py``, so a test double or an
    audit like this one is not mistaken for a production reach. ``_stubs/`` is
    NOT skipped: a stub that reaches another stub is a reach, and the point of
    the enumeration is that every one of them has a written reason.
    """
    base = _REPO / module
    if not base.is_dir():
        return []
    out: List[Path] = []
    for path in sorted(base.rglob("*.py")):
        rel = path.relative_to(_REPO)
        if set(rel.parts[:-1]) & _SKIP_DIRS:
            continue
        if not include_tests and path.name.startswith("test_"):
            continue
        out.append(path)
    return out


def _stub_reach_sites(*, include_tests: bool = False) -> List[Tuple[str, int, str]]:
    """Return every place a ``harness._stubs`` module can be imported.

    Three syntactic forms, because the reach can be spelled three ways:
    ``from harness._stubs import sandbox``, ``import harness._stubs.sandbox``,
    and ``__import__("harness._stubs...")``. A plain grep over the string
    ``harness._stubs`` would also match prose in a docstring, so this is an AST
    pass over import statements only.
    """
    sites: List[Tuple[str, int, str]] = []
    for module in PACKAGED_MODULES:
        for path in _python_files(module, include_tests=include_tests):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            rel = path.relative_to(_REPO).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    name = node.module or ""
                    if name.startswith("harness._stubs"):
                        sites.append((rel, node.lineno, f"from {name} import ..."))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("harness._stubs"):
                            sites.append((rel, node.lineno, f"import {alias.name}"))
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "__import__"
                ):
                    for argument in node.args:
                        if (
                            isinstance(argument, ast.Constant)
                            and isinstance(argument.value, str)
                            and argument.value.startswith("harness._stubs")
                        ):
                            sites.append(
                                (rel, node.lineno, f"__import__({argument.value!r})")
                            )
    return sorted(sites)


#: Every reachable site, written down. The set is CLOSED: a fourth entry fails
#: this pin, which is the audit's whole value — an audit that is re-derived by
#: reading is an audit that gets forgotten.
#:
#: The SANDBOX stub specifically is the one this module's inverted pin is about,
#: so the other three stubs ``deps.py`` also reaches are enumerated here too.
#: They are documented rather than ignored: the audit's claim is "here is
#: every way a stub can be selected", not "there is only one stub".
#:
#: 1. ``harness/agent_loop.py`` — **LIVE and UNGUARDED.** The unsandboxed bash
#:    path for ``legacy_agent``. Owner: T1 / P2.1. This is what the inverted pin
#:    below fails on, and it is the only LIVE site.
#: 2. ``harness/deps.py`` (4 sites) - the Boundary resolvers. Each reaches its stub
#:    only when BOTH an explicit env opt-in is set AND the real package is
#:    absent, so these are deliberate development lanes, not silent ones:
#:    ``sandbox`` (Boundary 1), ``model_router`` (Boundary 2), ``verify``
#:    (the verifier stub), ``scripted_model`` (a deterministic TEST hook,
#:    reached only via the ``HARNESS_SCRIPTED_MODEL`` env var).
#: 3. ``harness/_stubs/verify.py`` — the stub verifying itself with the stub
#:    sandbox. Inside ``_stubs/``; only reachable once the stub was chosen.
EXPECTED_STUB_REACH_SITES: Tuple[Tuple[str, str], ...] = tuple(
    sorted(
        {
            ("harness/_stubs/verify.py", "from harness._stubs.sandbox import ..."),
            ("harness/agent_loop.py", "from harness._stubs import ..."),
            ("harness/deps.py", "from harness._stubs.model_router import ..."),
            ("harness/deps.py", "from harness._stubs.sandbox import ..."),
            ("harness/deps.py", "from harness._stubs.scripted_model import ..."),
            ("harness/deps.py", "from harness._stubs.verify import ..."),
        }
    )
)


def test_the_stub_reach_set_is_closed() -> None:
    """Every way the local-subprocess stub is reachable, written down.

    Green today with exactly three sites, one of which is the known live hole.
    A NEW site fails here rather than appearing in a handoff six months from
    now.
    """
    actual = tuple(
        sorted(
            {
                (path, what)
                for path, _line, what in _stub_reach_sites(include_tests=False)
            }
        )
    )
    assert actual == EXPECTED_STUB_REACH_SITES, (
        "the set of ways harness._stubs can be reached changed.\n"
        f"  expected: {list(EXPECTED_STUB_REACH_SITES)}\n"
        f"  actual:   {list(actual)}\n"
        "Update EXPECTED_STUB_REACH_SITES with a reason, and re-run the "
        "inverted pin below if the new site is a live one."
    )
    # Non-vacuity: the scan must actually find something. A closed set of three
    # asserted against a scan that returns nothing would be indistinguishable
    # from a closed set of nothing.
    assert len(_stub_reach_sites(include_tests=False)) >= 3


def test_the_execution_package_never_imports_a_stub() -> None:
    """``execution/**`` PRODUCTION source has ZERO reach into ``harness._stubs``.

    The production execution layer must not be able to select the stub for
    itself. It imports ``harness.tool_errors``, ``harness.test_config`` and
    ``harness.webfetch`` (all fail-closed helper modules); a future edit that
    added ``harness._stubs.sandbox`` here would fail this test.
    """
    execution_files = {
        path.relative_to(_REPO).as_posix()
        for path in _python_files("execution", include_tests=False)
    }
    offences = [
        f"{path}:{line} {what}"
        for path, line, what in _stub_reach_sites(include_tests=False)
        if path in execution_files
    ]
    assert not offences, (
        f"execution/** reached a harness stub: {offences!r}. The production "
        "execution layer selects its own boundary; it never falls back to a "
        "local subprocess"
    )


def _list_head(sequence: ast.AST) -> str | None:
    """Return the first literal element of a list/tuple, if there is one."""
    if isinstance(sequence, (ast.List, ast.Tuple)) and sequence.elts:
        first = sequence.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _subprocess_calls(scope: ast.AST) -> List[ast.Call]:
    """Return every ``subprocess.*`` invocation inside ``scope``."""
    found: List[ast.Call] = []
    for node in ast.walk(scope):
        func = getattr(node, "func", None)
        if (
            isinstance(node, ast.Call)
            and isinstance(func, ast.Attribute)
            and func.attr in ("run", "Popen", "call", "check_output", "check_call")
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            found.append(node)
    return found


def _spawned_argv0(call: ast.Call, scope: ast.AST) -> Tuple[str | None, bool]:
    """Return ``(argv[0], resolved)`` for one ``subprocess`` call.

    The sandbox module builds its argv through local list variables
    (``command = ["docker", *args]`` then ``subprocess.Popen(command, ...)``),
    so a literal-only scan would report every real call as unresolvable and the
    pin would be theatre. Local assignments are resolved one level deep, which
    is exactly the shape the module uses.
    """
    argv_node = call.args[0] if call.args else None
    if isinstance(argv_node, ast.Constant) and isinstance(argv_node.value, str):
        return argv_node.value, True
    literal = _list_head(argv_node) if argv_node is not None else None
    if literal is not None:
        return literal, True
    if isinstance(argv_node, ast.Name):
        # Match by NAME, not by node identity: `ast.AST` has no `__eq__`, so
        # comparing the load-site Name against the assignment target Name would
        # never match and every real call would report UNRESOLVED.
        wanted = argv_node.id
        for node in ast.walk(scope):
            targets: List[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.target is not None:
                targets, value = [node.target], node.value
            if value is None:
                continue
            if any(
                isinstance(target, ast.Name) and target.id == wanted
                for target in targets
            ):
                return _list_head(value), True
    return None, False


def test_the_execution_sandbox_spawns_exactly_one_program_and_it_is_docker() -> None:
    """Argv-level pin: ``execution/sandbox.py`` can only spawn ``docker``.

    A containment receipt is a claim about what the daemon was asked to do. A
    local-subprocess fallback added anywhere in this module would make that
    claim false while every mount assertion in
    ``execution/test_containment_pins.py`` still passed, because those
    assertions inspect an argv that a host process never receives.

    Each function is scanned against its OWN scope, so an assignment in one
    function cannot resolve a call in another.
    """
    path = _REPO / "execution" / "sandbox.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    parent_of: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent_of[child] = node

    def owning_scope(node: ast.AST) -> ast.AST:
        """Return the NEAREST enclosing function, or the module."""
        current = node
        while current in parent_of:
            current = parent_of[current]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
        return tree

    offenders: List[str] = []
    inspected = 0
    for call in _subprocess_calls(tree):
        inspected += 1
        argv0, resolved = _spawned_argv0(call, owning_scope(call))
        if not resolved:
            offenders.append(f"line {call.lineno}: argv[0] UNRESOLVED")
        elif argv0 != "docker":
            offenders.append(f"line {call.lineno}: argv[0]={argv0!r}")
    assert inspected >= 1, (
        "the scan found no subprocess call at all; it has stopped working"
    )
    assert not offenders, (
        "execution/sandbox.py can spawn a program other than the docker CLI: "
        f"{offenders!r}. A local subprocess here would be an unsandboxed run "
        "that every other pin in execution/ cannot see"
    )


def test_an_unreachable_daemon_raises_and_never_runs_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correct behaviour, proven: fail loud, never run on the host.

    ``execute_sandboxed`` must RAISE, and must do so BEFORE it spawns anything.
    The sentinel file is the control: if a fallback existed, the command would
    have written it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    sentinel = repo / "witness.txt"

    import execution.sandbox as sandbox_module

    # Both the availability probe and the spawn are replaced, so a fallback
    # that ignored the probe would still be caught by the exploding spawn.
    monkeypatch.setattr(sandbox_module, "docker_available", lambda: False)
    monkeypatch.setattr(
        sandbox_module,
        "_run_docker",
        _explode("the docker CLI was spawned with no daemon reachable"),
    )

    with pytest.raises(SandboxUnavailableError) as caught:
        execute_sandboxed(str(repo), f"touch {sentinel.name} && echo ran", 30)
    message = str(caught.value)
    assert "unsandboxed" in message.lower(), message
    assert not sentinel.exists(), (
        "the command RAN despite an unreachable daemon; that is the silent "
        "unsandboxed fallback this test exists to forbid"
    )


def test_the_sandbox_does_not_expose_any_local_execution_entry_point() -> None:
    """``execution.sandbox`` must not even OFFER a host-subprocess call.

    A public ``execute_locally`` / ``run_on_host`` next to
    ``execute_sandboxed`` is an invitation, and an invitation becomes a call
    site the first time someone needs a Docker-less machine.
    """
    import execution.sandbox as sandbox_module

    forbidden = {
        "execute_locally",
        "execute_local",
        "execute_unsandboxed",
        "execute_on_host",
        "run_on_host",
        "run_locally",
        "local_fallback",
        "fallback_to_local",
    }
    exported = set(dir(sandbox_module))
    assert not (exported & forbidden), (
        "execution.sandbox gained a host-execution entry point: "
        f"{sorted(exported & forbidden)!r}. A Docker-less machine is served by "
        "catching SandboxUnavailableError and choosing a profile explicitly, "
        "not by a second entry point that looks equivalent"
    )


def test_the_deps_stub_fallback_needs_an_explicit_opt_in() -> None:
    """``harness/deps.py`` may reach the stub, but only deliberately.

    Two INDEPENDENT conditions, both required: an explicit env opt-in AND the
    real boundary package being absent. One condition would make a stray
    environment variable silently select the unsandboxed lane.
    """
    import harness.deps as deps_module

    assert tuple(deps_module._STUB_ENV_NAMES) == (
        "HARNESS_USE_STUBS",
        "HARNESS_ALLOW_STUB_FALLBACK",
    )

    source = (_REPO / "harness" / "deps.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename="harness/deps.py")
    resolver = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "get_execute_sandboxed"
    )
    handlers = [
        node
        for node in ast.walk(resolver)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "ModuleNotFoundError"
    ]
    assert len(handlers) == 1, (
        f"expected one ModuleNotFoundError handler, got {len(handlers)}"
    )
    handler_text = ast.dump(handlers[0])
    assert "_stub_fallback_allowed" in handler_text, (
        "the stub fallback is no longer gated on the explicit opt-in"
    )
    assert "_boundary_missing" in handler_text, (
        "the stub fallback no longer checks that the real boundary is ABSENT"
    )

    # ...and the gate is OFF by default, so a run with no environment set cannot
    # reach the stub even if `execution` were absent.
    assert deps_module._stub_fallback_allowed() in (True, False)


def test_the_stub_declares_that_it_is_not_a_boundary() -> None:
    """Record what the stub IS, so nobody mistakes it for the real thing.

    The stub runs a host subprocess, so it cannot have a read-only ``.git``, a
    network namespace boundary, or a resource limit. A docstring is not
    evidence of behaviour, so the ABSENCE of the containment surface is
    asserted too — that half is what makes the docstring credible.
    """
    from harness._stubs import sandbox as stub

    assert stub.execute_sandboxed.__module__ == "harness._stubs.sandbox"
    for absent in (
        "ContainmentPolicy",
        "resolve_containment",
        "readonly_overlays",
        "assert_sandbox_argv_isolated",
        "own_container_filter",
        "SandboxUnavailableError",
    ):
        assert not hasattr(stub, absent), (
            f"the local-subprocess stub gained {absent!r}; a stub that looks "
            "contained is worse than one that plainly is not"
        )
    text = ((stub.__doc__ or "") + (stub.execute_sandboxed.__doc__ or "")).lower()
    assert "stub" in text
    assert "isolation" in text or "uncontained" in text, (
        "the stub's own documentation must state that it provides no isolation"
    )


# ---------------------------------------------------------------------------
# THE INVERTED PIN — red by design
# ---------------------------------------------------------------------------

#: T5's known-failing registry entry. The name is stable and is what a reader
#: of a red CI run will search for.
INVERTED_PIN_NAME = (
    "test_RED_BY_DESIGN_no_production_import_path_reaches_"
    "the_local_subprocess_sandbox_stub"
)

#: The two sites the inverted pin permits, by exact path. Both have a written
#: reason in :data:`EXPECTED_STUB_REACH_SITES`. Note the pin filters by PATH,
#: not by the specific import: ``harness/deps.py`` reaches FOUR stubs and all
#: four are env-gated, so excluding the file is the honest granularity.
NON_LIVE_REACH_SITES: Tuple[str, ...] = ("harness/_stubs/verify.py", "harness/deps.py")


def _explode(message: str):
    """Return a callable that raises, so a spawn attempt is unmissable."""

    def _raise(*_args, **_kwargs):
        raise AssertionError(message)

    return _raise


def test_RED_BY_DESIGN_no_production_import_path_reaches_the_local_subprocess_sandbox_stub() -> (
    None
):
    """THIS TEST IS RED BY DESIGN.

    **It goes green when ``harness/agent_loop.py`` deletes the unsandboxed
    bash path. Owner: T1 / P2.1.**

    Registered with T5 as KNOWN-FAILING. **Do not "fix" it by suppressing it,
    by adding an xfail marker, by deleting it, or by adding the offending path
    to :data:`NON_LIVE_REACH_SITES`.** The only change that turns it green is
    deleting ``harness/agent_loop.py``'s import of ``harness._stubs.sandbox``
    and the local-subprocess ``execute_sandboxed`` call that follows it.

    Exact test name for T5's registry::

        execution/test_unsandboxed_pins.py::test_RED_BY_DESIGN_no_production_import_path_reaches_the_local_subprocess_sandbox_stub

    What it asserts, and why the assertions are the two the brief names::

        assert no live code path invokes execute_sandboxed's stub fallback
        assert the local-subprocess stub is unreachable from a non-test import path

    The reach set is enumerated by AST over every packaged module, so all three
    syntactic ways to spell the import are caught and a dynamically constructed
    one is caught too. The two non-live sites are excluded by an exact, closed
    list with written reasons — the stub verifying itself, and the env-gated
    Boundary-1 resolver — and ``harness/agent_loop.py`` is not on it.
    """
    live_reaches = [
        (path, line, what)
        for path, line, what in _stub_reach_sites(include_tests=False)
        if path not in NON_LIVE_REACH_SITES
    ]
    assert not live_reaches, (
        "\n"
        "  THE UNSANDBOXED BASH PATH IS STILL PRESENT.\n"
        "\n"
        "  A non-test import path can still reach `harness._stubs.sandbox`, the\n"
        "  local-subprocess sandbox that runs bash on the live host with no\n"
        "  container, no read-only `.git`, no network namespace and no resource\n"
        '  limits. `harness/AGENTS.md` records this as "the one place the\n'
        '  containment fix did not reach".\n'
        "\n"
        "  Offending sites:\n"
        + "".join(f"    {path}:{line}  {what}\n" for path, line, what in live_reaches)
        + "\n"
        "  Owner: T1 (harness/agent_loop.py), removal tracked as P2.1.\n"
        "  This pin exists so that deletion is PROVABLE: when the path is gone,\n"
        "  this test turns green with no other change anywhere in the repo."
    )


def test_the_inverted_pin_is_registered_as_known_failing_and_not_suppressed() -> None:
    """The pin's own registration, so it cannot be quietly dropped or muted.

    Checked by AST rather than by searching this file's text, because the
    suppression keywords necessarily appear in this file's own source — a
    substring scan would match itself and pass vacuously.
    """
    known_failing: Dict[str, str] = {
        INVERTED_PIN_NAME: (
            "T1 / P2.1 - harness/agent_loop.py runs bash through "
            "harness._stubs.sandbox on the live host for the legacy_agent "
            "strategy. Green when that path is deleted."
        ),
    }
    tree = ast.parse(
        _HERE.joinpath("test_unsandboxed_pins.py").read_text(encoding="utf-8-sig"),
        filename="test_unsandboxed_pins.py",
    )
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    for name, reason in known_failing.items():
        assert name in functions, (
            f"the known-failing pin {name!r} is not defined in this module; a "
            "known-failing registry pointing at a test that does not exist is "
            "how a real failure gets filed as a known one"
        )
        assert len(reason) >= 60, f"the reason for {name!r} is not a reason: {reason!r}"

        node = functions[name]
        for decorator in node.decorator_list:
            rendered = ast.dump(decorator)
            assert "xfail" not in rendered, (
                "the inverted pin must not carry an xfail marker; T5's "
                "known-failing registry is the SINGLE place a red test is "
                "recorded, and an xfail in place makes it indistinguishable "
                "from a pin that was fixed"
            )
            assert "skipif" not in rendered and "skip" not in rendered, (
                "the inverted pin must not be skipped in place; a skipped pin "
                "is indistinguishable from a fixed one"
            )

    module_doc = ast.get_docstring(tree) or ""
    assert "RED BY DESIGN" in module_doc, (
        "the module no longer declares that it is red by design; a reader who "
        "finds it failing must be able to learn why from here rather than "
        "concluding that something regressed"
    )
    # The allowed non-live sites must stay a CLOSED, exact list.
    assert NON_LIVE_REACH_SITES == ("harness/_stubs/verify.py", "harness/deps.py")
    assert set(NON_LIVE_REACH_SITES).issubset(
        {path for path, _what in EXPECTED_STUB_REACH_SITES}
    )
