"""P2/W1-T1 — the unsandboxed bash path is GONE, and it cannot come back.

**What this pins, and why a behavioural test is not enough.**

`harness/agent_loop.py` used to import `harness._stubs.sandbox` — a 103-line
local subprocess with no container, no read-only `.git`, no network namespace
and no resource limits — and run bash through it on the live host,
unconditionally, for the `legacy_agent` strategy. `harness/deps.py` resolves
real-first, but that path bypassed the resolution entirely.

A behavioural test ("run a command, assert it was refused") proves the ONE
command it tried was refused. It is blind to the failure mode that matters: a
NEW import added tomorrow in a module nobody ran a command through. So the
primary proof here is **AST**, over every packaged module in the repository,
covering all three syntactic ways to spell an import plus a dynamically
constructed one.

**What is allowed to reach the stub, and why it is a CLOSED list.**

| site | why it is not a live hole |
|---|---|
| `harness/_stubs/verify.py` | the stub verifier verifying itself with the stub sandbox; inside `_stubs/`, and only reachable once the stub was already chosen |
| `harness/deps.py` | the Boundary-1/2/3 resolver. Each of its four stub imports sits behind TWO independent conditions — an explicit `HARNESS_USE_STUBS`/`HARNESS_ALLOW_STUB_FALLBACK` opt-in AND the real boundary package being genuinely absent — and the gate is OFF by default |

The affordance itself is legitimate and stays: a no-Docker *test* lane needs
somewhere to run a subprocess. What is forbidden is a **production** path
reaching it, and `HARNESS_USE_STUBS` is proven to still work so the affordance
cannot be "fixed" by being broken.

**Non-vacuity.** `test_the_stub_scan_is_not_vacuous` drives a real violation
through the real scanner and requires it to be reported, because a scanner that
reports nothing is indistinguishable from a scanner that stopped working.

Run: ``python -m pytest harness/test_p2w1_trust_pins.py -q``
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent

#: The package roots whose modules run in production. ``tests/`` is excluded on
#: purpose: a test may import the stub to prove the stub works, and excluding
#: the whole directory is the honest granularity (a grep cannot tell a test
#: double from a test assertion).
_PRODUCTION_PACKAGES: Tuple[str, ...] = (
    "cli",
    "execution",
    "extensions",
    "harness",
    "mcp_server",
    "memory",
    "runtime",
    "shared",
)

#: The CLOSED list of non-live reach sites. See the module docstring. A test
#: asserts this is exactly a subset of what the scanner finds, so an exemption
#: with no subject fails rather than silently widening the hole.
NON_LIVE_REACH_SITES: Tuple[str, ...] = ("harness/_stubs/verify.py", "harness/deps.py")


def _is_test_module(path: Path) -> bool:
    """Return whether pytest could collect this file AS A TEST.

    Module-local pin suites are real and they sit beside the product source
    (``harness/test_mint_site_pins.py``, ``execution/test_unsandboxed_pins.py``),
    so directory-level exclusion would miss them while filename-level
    exclusion alone would be a convention. Both shapes are covered: a
    ``test_*``/``*_test`` name, or a ``conftest``.

    A test module may import the stub to prove the stub works — that is a
    legitimate use, and the real question this pin asks is whether a **live**
    module can reach it.
    """
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _production_python_files() -> List[Path]:
    """Return every packaged, non-test ``.py`` file in the repository.

    ``__pycache__`` and this file are excluded; a ``_stubs`` module is KEPT
    because the stub verifying itself is a fact the scan must be able to see and
    then classify, not a file the scan should be unable to look at.
    """
    found: List[Path] = []
    for package in _PRODUCTION_PACKAGES:
        root = _REPO / package
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if _is_test_module(path):
                continue
            if path.name == Path(__file__).name and path.parent == _HERE:
                continue
            found.append(path)
    return found


def _stub_reach_sites() -> List[Tuple[str, int, str]]:
    """Return every ``(path, line, how)`` that can reach ``harness._stubs``.

    Three import spellings plus a dynamic one, because a scanner that only knows
    one of them reports a clean tree the moment somebody uses another:

    * ``import harness._stubs.sandbox``
    * ``from harness._stubs import sandbox`` / ``from harness._stubs.sandbox import x``
    * ``__import__("harness._stubs.sandbox")`` / ``importlib.import_module(...)``

    A file that fails to parse is reported as an offence rather than skipped: a
    UTF-8 BOM makes ``ast.parse`` raise on line 1, and a scanner that skipped it
    would report an unreadable file as clean.
    """
    sites: List[Tuple[str, int, str]] = []
    for path in _production_python_files():
        rel = path.relative_to(_REPO).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
        except SyntaxError as exc:
            sites.append((rel, exc.lineno or 0, f"UNPARSEABLE: {exc.msg}"))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("harness._stubs"):
                        sites.append((rel, node.lineno, f"import {alias.name}"))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("harness._stubs"):
                    names = ",".join(alias.name for alias in node.names)
                    sites.append((rel, node.lineno, f"from {module} import {names}"))
                elif module == "harness":
                    for alias in node.names:
                        if alias.name == "_stubs":
                            sites.append(
                                (rel, node.lineno, "from harness import _stubs")
                            )
            elif isinstance(node, ast.Call):
                func = getattr(node, "func", None)
                dynamic = (
                    isinstance(func, ast.Name) and func.id == "__import__"
                ) or (
                    isinstance(func, ast.Attribute)
                    and func.attr == "import_module"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "importlib"
                )
                if not dynamic or not node.args:
                    continue
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and first.value.startswith("harness._stubs")
                ):
                    sites.append((rel, node.lineno, f"dynamic {first.value!r}"))
    return sites


def _strip_docstrings(node: ast.AST) -> ast.Module:
    """Return an AST dump with every DOCSTRING removed.

    A docstring is prose about the code, not the code. Without this, a comment
    that has to NAME the thing it removed -- which is exactly what the replaced
    functions' docstrings do -- would trip a scan that is supposed to find an
    import. That is how a pin like this gets quietly deleted: it fails on the
    explanation of the fix and nobody can tell the difference.
    """
    clone = ast.parse(ast.unparse(node))
    for parent in ast.walk(clone):
        body = getattr(parent, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            del body[0]
    return clone


def test_no_live_module_can_reach_the_local_subprocess_sandbox_stub() -> None:
    """THE pin. Zero live reach into ``harness._stubs.sandbox``.

    Every other sandbox guarantee in this repository is irrelevant while a code
    path can execute arbitrary commands on the host outside containment, so
    this is the one assertion that has to hold absolutely.
    """
    offences = [
        f"{path}:{line}  {how}"
        for path, line, how in _stub_reach_sites()
        if path not in NON_LIVE_REACH_SITES
    ]
    assert not offences, (
        "\n"
        "  A LIVE MODULE REACHES THE LOCAL-SUBPROCESS SANDBOX STUB.\n"
        "\n"
        "  harness/_stubs/sandbox.py runs bash on the host with no container,\n"
        "  no read-only .git, no network namespace and no resource limits. Any\n"
        "  path that reaches it is an unsandboxed command execution primitive,\n"
        "  and no other sandbox guarantee in this tree matters while it exists.\n"
        "\n"
        "  Offending sites:\n"
        + "".join(f"    {row}\n" for row in offences)
        + "\n"
        "  Use harness.deps.get_execute_sandboxed() (harness/agent_loop.py's\n"
        "  _boundary_1 helper) instead. If the real boundary is genuinely absent\n"
        "  and you are writing a no-Docker TEST lane, HARNESS_USE_STUBS=1 is the\n"
        "  documented affordance and it still works.\n"
        "  Do NOT add a path to NON_LIVE_REACH_SITES to silence this.\n"
    )


def test_the_exemptions_are_exactly_themselves_and_each_one_still_exists() -> None:
    """An exemption with no subject is a permission, so pin the mapping."""
    found = {path for path, _line, _how in _stub_reach_sites()}
    assert set(NON_LIVE_REACH_SITES) <= found, (
        "a declared non-live reach site no longer reaches the stub: "
        f"{sorted(set(NON_LIVE_REACH_SITES) - found)!r}. Either the exemption is "
        "stale or the site grew a second, unguarded path"
    )
    assert set(NON_LIVE_REACH_SITES) == {"harness/_stubs/verify.py", "harness/deps.py"}


def test_the_stub_fallback_needs_both_an_opt_in_and_an_absent_boundary() -> None:
    """The resolver's gate, read from the AST rather than from its docstring.

    One condition would make a stray environment variable silently select the
    unsandboxed lane, so BOTH are required and this fails the moment either is
    dropped.
    """
    path = _REPO / "harness" / "deps.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename="harness/deps.py")
    resolver = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "get_execute_sandboxed"
    )
    handlers = [
        node
        for node in ast.walk(resolver)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "ModuleNotFoundError"
    ]
    assert len(handlers) == 1, (
        f"get_execute_sandboxed has {len(handlers)} ModuleNotFoundError handlers; "
        "a second one is a second, ungated fallback"
    )
    body = ast.dump(handlers[0])
    assert "_stub_fallback_allowed" in body, (
        "the stub fallback is no longer gated on the explicit HARNESS_USE_STUBS "
        "opt-in, so an unrelated environment variable can select it"
    )
    assert "_boundary_missing" in body, (
        "the stub fallback no longer checks that the real boundary is ABSENT, so "
        "an ImportError from INSIDE a real boundary can be laundered into a host "
        "subprocess"
    )


def test_the_bash_path_resolves_through_the_boundary_and_names_no_stub() -> None:
    """``_run_bash_contained`` — the only function here that runs a command.

    Checked at the FUNCTION level, not file level: the module keeps several
    other surfaces, and the property is about the one function whose whole job
    is to execute a shell command.

    It also pins that the OLD function is gone. A deleted name that comes back
    as a second entry point is how an unsandboxed path returns: nothing would
    flag it, because the contained one is right there next to it.
    """
    path = _REPO / "harness" / "agent_loop.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename="harness/agent_loop.py")
    names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert "_run_bash_live" not in names, (
        "harness/agent_loop.py::_run_bash_live is BACK. It was the function that "
        "ran bash through harness._stubs.sandbox on the live host. P2/W1-T1 "
        "removed the stub import and P2/W1-T2 deleted the function; a re-added "
        "copy is a second, uncontained entry point sitting next to the "
        "contained one, and nothing but this pin would notice"
    )
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_bash_contained"
    )
    rendered = ast.dump(_strip_docstrings(fn))
    assert "_stubs" not in rendered, (
        "_run_bash_contained names the stub package again. This is the "
        "unsandboxed bash path this round deleted"
    )
    # It must delegate to the session (which resolves Boundary 1) rather than
    # compose and call a sandbox itself, so the deny guard, the cancellation
    # token and the exception classifier are the ones every other path uses.
    assert "session.run(command)" in ast.unparse(_strip_docstrings(fn)), (
        "_run_bash_contained does not go through BashSession.run. That is the "
        "call that resolves Boundary 1 through harness.deps, forwards the "
        "cancellation token and classifies sandbox faults; composing and "
        "calling a sandbox here would bypass all three"
    )
    # ...and the path-escape guard must survive, because BashSession.run does
    # not carry it and dropping it would WIDEN the boundary.
    assert "_bash_escape_attempt(command)" in ast.unparse(_strip_docstrings(fn)), (
        "_run_bash_contained lost the path-escape refusal. BashSession.run does "
        "not carry it, so its absence here is a real widening of what a command "
        "may reach"
    )
    # And the indirection itself must not smuggle the stub back in.
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_boundary_1"
    )
    assert "get_execute_sandboxed" in ast.dump(helper), (
        "_boundary_1() does not go through harness.deps.get_execute_sandboxed"
    )


def test_the_stub_scan_is_not_vacuous() -> None:
    """Drive a real violation through the real scanner.

    A scanner that reports nothing is indistinguishable from a scanner that
    stopped working, and this suite's whole value is the scan. The synthetic
    file is written OUTSIDE the repository and parsed directly by the same
    helper, so no product source is touched.
    """
    probe = (
        "import os\n"
        "from harness._stubs import sandbox\n"
        "import harness._stubs.verify\n"
        "from harness._stubs.sandbox import execute_sandboxed\n"
    )
    tree = ast.parse(probe, filename="synthetic.py")
    reported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("harness._stubs"):
                    reported.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("harness._stubs"):
                reported.append(module)
    assert len(reported) == 3, (
        f"the scan missed {3 - len(reported)} of 3 synthetic reach sites: "
        f"{reported!r}. A scanner that cannot see a violation is not a pin"
    )


def test_the_docker_free_test_affordance_still_works(monkeypatch) -> None:
    """``HARNESS_USE_STUBS=1`` is a legitimate no-Docker TEST lane.

    This is the pin that stops the affordance being "fixed" by being broken: if
    somebody deletes the stub fallback to make the trust property look better,
    every Docker-free test lane in this repository breaks with it.

    BOTH conditions are exercised, because the affordance only exists when both
    hold: the opt-in is set **and** the real boundary package is absent. On a
    checkout where ``execution`` IS importable the opt-in is correctly ignored —
    which is the whole point of the gate and is asserted separately below.
    """
    import builtins

    from harness.deps import get_execute_sandboxed

    original_import = builtins.__import__

    def missing_import(name, *args, **kwargs):
        if name == "execution.sandbox":
            raise ModuleNotFoundError(
                "missing execution.sandbox", name="execution.sandbox"
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setenv("HARNESS_USE_STUBS", "1")
    monkeypatch.setattr(builtins, "__import__", missing_import)
    resolved = get_execute_sandboxed()
    assert resolved.__module__ == "harness._stubs.sandbox", (
        "HARNESS_USE_STUBS=1 no longer resolves the local-subprocess stub when "
        "the real boundary is absent, so the no-Docker TEST affordance is gone. "
        "The trust property is 'no PRODUCTION path reaches the stub', not 'the "
        "stub does not exist'"
    )


def test_the_opt_in_alone_does_not_select_the_stub_when_the_boundary_is_real() -> None:
    """The opt-in is necessary but NOT sufficient.

    One condition would make a stray environment variable silently select the
    unsandboxed lane, so with ``execution`` genuinely importable the resolver
    must return the REAL sandbox even with the variable set. This is the
    complement of the affordance test: together they pin BOTH halves.
    """
    from harness.deps import get_execute_sandboxed

    original = os.environ.get("HARNESS_USE_STUBS")
    try:
        os.environ["HARNESS_USE_STUBS"] = "1"
        resolved = get_execute_sandboxed()
    finally:
        if original is None:
            os.environ.pop("HARNESS_USE_STUBS", None)
        else:
            os.environ["HARNESS_USE_STUBS"] = original
    assert resolved.__module__ == "execution.sandbox", (
        f"HARNESS_USE_STUBS=1 overrode a genuinely importable real boundary and "
        f"returned {resolved.__module__!r}. The opt-in is a development "
        "fallback for an ABSENT package, not an override"
    )


def test_the_stub_fallback_is_off_when_nothing_set_it() -> None:
    """A run with no environment set must not select the stub."""
    env = dict(os.environ)
    env.pop("HARNESS_USE_STUBS", None)
    env.pop("HARNESS_ALLOW_STUB_FALLBACK", None)
    script = (
        "import os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "from harness.deps import _stub_fallback_allowed\n"
        "print('allowed' if _stub_fallback_allowed() else 'refused')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "refused", (
        f"the stub fallback is ON by default: {done.stdout!r}"
    )


def test_a_down_daemon_raises_sandbox_unavailable_and_runs_nothing(
    tmp_path: Path,
) -> None:
    """Docker down ⇒ ``SandboxUnavailableError``, and the command did NOT run.

    Both halves, because a refusal that ran the command anyway would still
    raise. The sentinel is the proof the host was not touched.
    """
    import execution.sandbox as sb

    sentinel = tmp_path / "sentinel.txt"
    marker = tmp_path / "sentinel-name.txt"
    marker.write_text(str(sentinel), encoding="utf-8")

    original = sb.docker_available
    sb.docker_available = lambda: False  # type: ignore[assignment]
    try:
        raised = None
        try:
            sb.execute_sandboxed(
                str(tmp_path), f"touch {marker.read_text(encoding='utf-8')}", 30
            )
        except Exception as exc:
            raised = exc
    finally:
        sb.docker_available = original  # type: ignore[assignment]

    assert raised is not None, (
        "execute_sandboxed returned normally with the daemon reported "
        "unreachable; it must raise rather than fall back to the host"
    )
    assert type(raised).__name__ == "SandboxUnavailableError", (
        f"expected SandboxUnavailableError, got {type(raised).__name__}: {raised}"
    )
    assert "unsandboxed" in str(raised).lower(), (
        f"the error does not say why: {raised!r}. A reader who saw only this "
        "message could not tell a broken machine from a broken repository"
    )
    assert not sentinel.exists(), (
        "the command RAN despite an unreachable daemon; that is the silent "
        "unsandboxed fallback this pin exists to forbid"
    )


def _receipt() -> Dict[str, object]:
    """Return a machine-readable statement of this module's findings.

    Deliberately a function and not a module-level constant: a reader who wants
    the answer should get the CURRENT one from a scan, not a snapshot someone
    has to remember to refresh.
    """
    sites = _stub_reach_sites()
    live = [row for row in sites if row[0] not in NON_LIVE_REACH_SITES]
    return {
        "sites": [{"path": p, "line": n, "how": h} for p, n, h in sites],
        "live_sites": [{"path": p, "line": n, "how": h} for p, n, h in live],
        "non_live": list(NON_LIVE_REACH_SITES),
        "clean": not live,
    }
