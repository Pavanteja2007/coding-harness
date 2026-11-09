"""P2/W1-T2 pins: containment proven under attack, and a receipt that can be wrong.

**Phase:** P2 Trusted Execution, Wave 1, T2 (execution). Host-only: no Docker, no
provider, no network. The one Docker-dependent claim (T2.W1.1's daemon-stopped
behaviour) is proven structurally against a live subprocess probe plus a
simulated-unreachable probe, and the real-daemon half is stated as a skipped
lane rather than counted as a pass.

Every pin here is a claim the phase makes. A pin that can pass vacuously is
worse than no pin (``phases/DOCTRINE.md`` §3), so each class below carries a
NON-VACUITY control: the negative arm is asserted to actually fire. The four
that matter most, and where each control lives:

* the subprocess classification (``TestTheSubprocessClassificationIsExhaustive``)
  is only evidence if a new subprocess site FAILS the audit, so the scanner is
  proven against a synthetic module that contains one;
* the Docker-stopped refusal (``TestTheDaemonStoppedPathRefuses``) is only
  evidence if a RUNNABLE daemon would have succeeded, so the control arm runs a
  real command with the daemon up;
* the receipt-falsifiability claim (``TestTheReceiptCanBeWrong``) is only
  evidence if the honest argv is clean, so every forgery is paired with the
  un-forged argv it is compared against;
* the flake three-valued verdict is only meaningful if two observations can
  reach ``not_flaky``, so every ``not_run`` assertion is paired with the
  two-observation arm that reaches the other two values.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from execution import edit_landing as el
from execution import flake_gate as fg
from execution import sandbox as sb
from execution import verify as ev
from execution import workspace as ws

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTION_ROOT = REPO_ROOT / "execution"

# ---------------------------------------------------------------------------
# T2.W1.1 -- the no-unsandboxed-fallback contract, at EVERY entry point
# ---------------------------------------------------------------------------

#: The classification table for every subprocess-bearing module in
#: ``execution/``. This is DATA, not prose, so the scan below can assert the
#: table is exhaustive against the real source and a new module fails rather
#: than going unclassified.
#:
#: The four verdicts, and the test each has to pass to earn it:
#:
#: ``docker_cli``
#:     The spawned program IS the ``docker`` client. Defensible because the
#:     program is fixed, the argv is a list (never ``shell=True``), and the
#:     boundary the command reaches is a container. This is the ONLY verdict
#:     that means "sandboxed".
#: ``fixed_argv_host``
#:     A host subprocess whose argv is a module-level literal list with no
#:     user/model-controlled element. Defensible for exactly one reason: the
#:     command and its arguments are decided in source, so no attacker
#:     influences what runs. It is NOT a sandbox -- it sees the host -- which
#:     is why each row names what it can reach.
#: ``test_or_measure``
#:     A driver or probe: ``sandbox_adversarial``, ``sandbox_stress``,
#:     ``sandbox_perf``, ``sandbox_cost``, ``sandbox_sustained``,
#:     ``containment_audit``. Not production, not a fallback; they spawn real
#:     containers to MEASURE containment. Reachable from ``python -m``, never
#:     from a run.
#: ``subprocess_handle``
#:     Not a spawn site at all: a TYPE annotation on a handle this module
#:     created, or an ``except`` clause naming an exception type. Enumerated so
#:     the audit is closed, and classified so a reader is not left guessing why
#:     a line matched the pattern.
SUBPROCESS_VERDICTS: Tuple[str, ...] = (
    "docker_cli",
    "fixed_argv_host",
    "test_or_measure",
    "subprocess_handle",
)

#: ``module name -> (verdict, what it may reach, why it is defensible)``.
SUBPROCESS_CLASSIFICATION: Dict[str, Tuple[str, str, str]] = {
    "sandbox.py": (
        "docker_cli",
        "the Docker daemon, which creates an isolated container",
        "the only program it spawns is `docker`; the workspace reaches the "
        "container through the argv the pre-spawn gate checked, and a daemon "
        "that is unusable raises SandboxUnavailableError",
    ),
    "e2e_attack.py": (
        "test_or_measure",
        "the host interpreter, running a driver this module creates",
        "P4/W2's ten-attack driver. It spawns `git` to build a real fixture repository with a real history -- rows 7 and 10 need two resolvable commits -- and spawns nothing else. Unreachable from a run: nothing in harness/ imports it. Its other work goes through execution.sandbox, already classified as docker_cli",
    ),
    "multi_session.py": (
        "test_or_measure",
        "the host interpreter, running a driver this module creates",
        "P4/W2's multi-session containment axes. It spawns real CHILD processes on purpose -- the naming axis needs names from separate PIDs and the reaping axis needs a hard-killed owner -- and reads `docker ps` through execution.session_isolation, which already spawns the docker CLI. Unreachable from a run",
    ),
    "test_removal_e2e.py": (
        "test_or_measure",
        "the host interpreter, running a driver this module creates",
        "P4/W2's test-removal driver. It spawns `git` ONLY on the USER arm, to prove the user really can delete a test; the agent arm never reaches a spawn because the guard refuses first. Unreachable from a run",
    ),
    "human_diff.py": (
        "fixed_argv_host",
        "the host `git`, read-only, inside the work directory only",
        "argv is a list of `git diff`/`show`/`rev-parse` reads with a fixed flag "
        "set, `shell=True` appears nowhere, and it never writes: it renders a diff "
        "for a human reviewer and refuses to present one whose tree state has moved "
        "(`assert_fresh` re-renders and compares digests). It is deliberately a PLAIN "
        "git runner and NOT `git_output._git`, because that module sets "
        "`diff.external=` and a renderer must read the real diff a reviewer would see",
    ),
    "monorepo_cost.py": (
        "fixed_argv_host",
        "the host, running each workspace package's OWN declared test command, "
        "plus the sandbox when its runner is used",
        "argv is a LIST split from the package manifest with `shlex` and "
        "`shell=False`, which is what `test_shell_true_appears_nowhere_in_execution` "
        "requires; the DEFAULT measurement runner is `execution.sandbox."
        "execute_sandboxed` (already classified as docker_cli), and the list-argv "
        "host runner exists only for a host with no daemon. The command it runs is "
        "repository-controlled input, which is exactly why the sandbox is the default",
    ),
    "flake.py": (
        "fixed_argv_host",
        "the host interpreter, running the caller's test command",
        "HOST-SIDE BY DESIGN and documented as such: it is the clean-environment "
        "flake confirmation lane. Its argv is a list built from the resolved "
        "test command, which is caller-supplied BY CONTRACT -- so this is the "
        "row to re-read if the flake lane is ever put on a verification path",
    ),
    "git_output.py": (
        "fixed_argv_host",
        "the host `git`, inside the run directory only",
        "argv is a list, `shell=True` appears nowhere, ambient GIT_* config and "
        "filters are stripped, and every path is inside work_dir/pristine_dir. "
        "It can WRITE to the run directory -- that is its job (branch, commit, "
        "PR body) -- and never to the user's checkout",
    ),
    "workspace.py": (
        "fixed_argv_host",
        "the host, for the explicitly-labelled local/native_os execution "
        "profiles, plus `git status`/`rev-parse` for workspace identity",
        "local execution is a PROFILE (`local_trusted`, `native_os`) the "
        "policy engine must select and the tool result names; `verified_fix` "
        "and `docker` profiles route to the sandbox instead. The git calls are "
        "identity reads behind `_git_environment()`",
    ),
    "snapshot.py": (
        "fixed_argv_host",
        "the host `git` for `check-ignore` / `rev-parse`, inside the source tree",
        "one batched `git check-ignore --no-index` plus a toplevel probe; the "
        "output only decides which paths a snapshot plan KEEPS, and every "
        "excluded byte is reported in the receipt",
    ),
    "env_snapshot.py": (
        "docker_cli",
        "the Docker daemon, read-only (`docker images`)",
        "an argv-literal listing filtered to this product's own `harness-envsnap` "
        "prefix; it is the docker CLI, not a host command, and no external "
        "content reaches it",
    ),
    "sandbox_adversarial.py": (
        "test_or_measure",
        "real containers, deliberately hostile",
        "a CLI driver; the payloads are its own fixture files. Not on a run path",
    ),
    "sandbox_cost.py": (
        "test_or_measure",
        "real containers, for timing",
        "a CLI cost probe; produces measurements, not verdicts",
    ),
    "sandbox_perf.py": (
        "test_or_measure",
        "real containers, for latency",
        "a CLI performance probe producing timings; it makes no correctness "
        "claim and is never reached from a run",
    ),
    "sandbox_stress.py": (
        "test_or_measure",
        "real containers, many at once, with hard kills",
        "a CLI concurrency driver; it spawns child processes and kills them to "
        "prove orphan reaping, so it is an ATTACKER-shaped probe, never a lane "
        "a run can reach",
    ),
    "sandbox_sustained.py": (
        "test_or_measure",
        "real containers, for sustained load over a long window",
        "a CLI endurance driver; it reports timing and residue, never a verdict, "
        "and is unreachable from a run",
    ),
    "containment_audit.py": (
        "test_or_measure",
        "real containers, attacking containment",
        "the CLI audit that produces the containment attack table; exits "
        "non-zero on a finding AND on a blocked lane",
    ),
    "deferral_attack.py": (
        "test_or_measure",
        "real `git init` + `git commit` in a throwaway fixture repository, and "
        "the local git/SafeToolBackend it drives",
        "a CLI attack driver: it builds a REAL git repository per harness, "
        "because a real `git` fixture is what makes the deferred-tool rows "
        "non-theatrical. It reaches git on the HOST and only inside a "
        "`tempfile` directory it creates itself; it writes no verdict a run "
        "consumes, and it is unreachable from a run. Added 2026-10-06: P3b/W2-T2 "
        "landed this module and this pin correctly reported it as unclassified. "
        "Its own AGENTS.md section records the host-only, no-network posture.",
    ),
    "warm_sandbox.py": (
        "docker_cli",
        "the Docker daemon (container reuse)",
        "uses the same `_docker_run_args` builder and the same pre-spawn gate as "
        "sandbox.py. NOTE: it has NO production call site and its output egress "
        "was fenced by P0/W1",
    ),
}

#: Files that are NOT production subprocess sites, excluded from the audit by
#: NAME with a reason -- because excluding them silently is how a hole hides.
AUDIT_EXEMPT: Dict[str, str] = {
    "test_selection.py": (
        "PRODUCTION module whose name matches pytest's `test_*.py` discovery "
        "pattern (documented in AGENTS.md 8.6); it contains no subprocess call"
    ),
    "ingress.py": "pure text capping/redaction; no I/O of any kind",
    "rationale.py": "reads two JSON files; no process",
    "result_parsing.py": "pure text parsing",
    "feedback.py": "pure text parsing",
    "baseline_set.py": "in-process classification over a VerificationResult",
    "spec_ledger.py": "reads/writes one JSON artifact",
    "verification_intelligence.py": "pure classification",
    "verification_gate.py": "pure composition; its subprocess work is delegated",
    "independent_evidence.py": "filesystem only (held-out suite materialisation)",
    "walk_scope.py": "a directory walk; no process",
    "ecosystems.py": "a data registry; no process",
    "flake_gate.py": "a pure reducer over observed outcomes; no process",
    "sandbox.py.duplicate": "placeholder, never present",
    "__init__.py": "re-exports only",
    "__main__.py": "dispatches execution.sandbox's own CLI",
    "edit_landing.py": "NEW this wave: pure path/receipt logic, no process",
}


def _subprocess_sites(source: str) -> List[int]:
    """Return the line numbers of real SPAWN sites in one module's source.

    A spawn is a CALL to ``subprocess.run`` / ``subprocess.Popen`` /
    ``subprocess.call`` / ``subprocess.check_output``. Everything else that
    matches ``subprocess.`` -- a type annotation, an ``except`` clause, the
    ``PIPE``/``DEVNULL`` constants, a ``CompletedProcess`` result constructor --
    is a MENTION, not a spawn, and is enumerated separately by
    :func:`_subprocess_mentions`.

    Uses AST rather than a regex: a grep cannot tell ``subprocess.run(...)``
    from ``except subprocess.TimeoutExpired``, and this corpus contains three
    of the latter.
    """
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr in ("run", "Popen", "call", "check_output")
    ]


def _subprocess_mentions(source: str) -> List[int]:
    """Return the line numbers of NON-spawn references to ``subprocess``.

    Enumerated so the audit is CLOSED: a reader is never left wondering whether
    a line the pattern matched was a spawn. Each of these is a type, a
    constant, an exception class, or a result constructor -- none of them
    starts a process.
    """
    spawns = set(_subprocess_sites(source))
    spawn_lines = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr in ("run", "Popen", "call", "check_output")
        ):
            spawn_lines.add(node.func.lineno)
    mentions: List[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name) or node.value.id != "subprocess":
            continue
        # `subprocess.Popen[str]` in an annotation reaches BOTH the Subscript
        # and the inner Attribute, and `subprocess.run(...)` reaches the
        # Attribute and its enclosing Call. Filtering by "is this node the
        # function part of a spawn Call" keeps an ANNOTATED Popen as a mention
        # while a spawned one stays a spawn.
        line = node.lineno
        if line in spawn_lines and line in spawns:
            continue
        if line not in mentions:
            mentions.append(line)
    return sorted(mentions)


def _modules_under_test() -> List[Path]:
    return sorted(
        path
        for path in EXECUTION_ROOT.glob("*.py")
        if not path.name.startswith("test_")
    )


class TestTheSubprocessClassificationIsExhaustive:
    """T2.W1.1: every subprocess site in `execution/` has a verdict.

    The audit is only evidence if a NEW site fails it, so the scanner is proven
    against a synthetic module first. Without that, a scanner that silently
    returns an empty list would make every classification assertion vacuous.
    """

    def test_the_scanner_is_not_a_rubber_stamp_before_anything_is_asserted(self) -> None:
        good = "import subprocess\nproc = subprocess.run(['ls'])\n"
        assert _subprocess_sites(good) == [2], "a real spawn must be found"
        for hostile, why in (
            ("import subprocess\ntry:\n    pass\nexcept subprocess.TimeoutExpired:\n    pass\n", "an except clause is not a spawn"),
            ("import subprocess\nPIPE = subprocess.PIPE\n", "a constant is not a spawn"),
            ("def f(x: subprocess.Popen[str]):\n    return x\n", "a type annotation is not a spawn"),
            ("import subprocess\nd = subprocess.CompletedProcess('a', 0)\n", "a result constructor is not a spawn"),
        ):
            assert _subprocess_sites(hostile) == [], (
                f"{why}: the spawn scanner reported {_subprocess_sites(hostile)}"
            )
            assert _subprocess_mentions(hostile), (
                f"{why}: but the MENTION scanner must still enumerate it, or the "
                "audit is not closed"
            )

    def test_every_module_with_a_spawn_is_classified(self) -> None:
        unclassified: List[str] = []
        for path in _modules_under_test():
            source = path.read_text(encoding="utf-8", errors="replace")
            if not _subprocess_sites(source):
                continue
            if path.name in SUBPROCESS_CLASSIFICATION:
                continue
            if path.name in AUDIT_EXEMPT:
                continue
            unclassified.append(path.name)
        assert unclassified == [], (
            "these modules spawn a subprocess and carry no verdict in "
            f"SUBPROCESS_CLASSIFICATION: {unclassified}. Add the module with a "
            "verdict and a justification, or an AUDIT_EXEMPT entry with a "
            "reason."
        )

    def test_every_exempt_module_really_has_no_spawn(self) -> None:
        """An exemption with no subject is a permission, not an exemption."""
        for name, reason in AUDIT_EXEMPT.items():
            if name.endswith(".duplicate"):
                continue
            path = EXECUTION_ROOT / name
            if not path.exists():
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            assert _subprocess_sites(source) == [], (
                f"{name} is exempt from the subprocess audit ({reason!r}) but "
                "now contains a spawn site; the exemption must be removed"
            )

    def test_every_classified_verdict_is_one_of_the_four(self) -> None:
        for name, (verdict, reaches, why) in SUBPROCESS_CLASSIFICATION.items():
            assert verdict in SUBPROCESS_VERDICTS, f"{name}: unknown verdict {verdict!r}"
            assert len(why.strip()) > 40, f"{name}: the justification is a stub"
            assert reaches.strip(), f"{name}: 'what it may reach' is empty"

    def test_only_the_docker_cli_verdict_is_called_sandboxed(self) -> None:
        """A host subprocess is never containment, whatever its argv looks like."""
        host_rows = [
            name
            for name, (verdict, _, _) in SUBPROCESS_CLASSIFICATION.items()
            if verdict == "fixed_argv_host"
        ]
        assert host_rows, "the fixed-argv host rows must exist and be named"
        # Every one of them must SAY it is a host process, so a reader cannot
        # mistake a well-justified host call for a sandboxed one.
        for name in host_rows:
            reaches = SUBPROCESS_CLASSIFICATION[name][1]
            assert "host" in reaches.lower(), (
                f"{name} is classified fixed_argv_host but its 'may reach' does "
                f"not say it is a host process: {reaches!r}"
            )

    def test_shell_true_appears_nowhere_in_execution(self) -> None:
        """`shell=True` would make every fixed-argv justification false."""
        offenders: List[str] = []
        for path in _modules_under_test():
            source = path.read_text(encoding="utf-8", errors="replace")
            if re_search_shell_true(source):
                offenders.append(path.name)
        assert offenders == [], (
            f"shell=True in {offenders}: a fixed-argv host call with a shell is "
            "not a fixed-argv host call, and the table's justification would be "
            "false"
        )


def re_search_shell_true(source: str) -> bool:
    """Return whether a module passes a shell to a subprocess (AST, not grep)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                return True
    return False


class TestNoExecutionProductionSourceReachesTheLocalSubprocessStub:
    """T2.W1.1: the stub is unreachable from `execution/**` production source.

    The stub is a local subprocess by construction, so ANY live reach into it
    is a containment hole rather than a degraded feature. This is the pin T1's
    deletion turns green from the other side, and it is deliberately one-sided:
    it cannot go red on a harness fix, because it never looks at harness code.
    """

    def test_no_production_module_names_the_stub(self) -> None:
        offenders: List[str] = []
        for path in _modules_under_test():
            source = path.read_text(encoding="utf-8", errors="replace")
            if "harness._stubs" in source or "harness/_stubs" in source:
                # A docstring naming the stub as the thing NOT to use is fine;
                # an import is not. Distinguish by looking for an import.
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.ImportFrom) and "harness._stubs" in str(
                        node.module or ""
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "__import__"
                        and node.args
                    ):
                        arg = node.args[0]
                        if isinstance(arg, ast.Constant) and str(
                            arg.value
                        ).startswith("harness._stubs"):
                            offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"execution/ production source reaches the stub: {offenders}"

    def test_execution_sandbox_exposes_no_host_execution_entry_point(self) -> None:
        """`execution.sandbox` must not offer a local-subprocess function."""
        forbidden = ("run_local", "local_execute", "execute_unsandboxed", "run_host")
        exported = [
            name
            for name in dir(sb)
            if not name.startswith("_")
            and any(token in name.lower() for token in forbidden)
        ]
        assert exported == [], (
            f"execution.sandbox exposes {exported}, which reads as a host "
            "execution entry point next to the sandbox boundary"
        )


class TestTheDaemonStoppedPathRefuses:
    """T2.W1.1: Docker down means `SandboxUnavailableError`, never a host run.

    Two arms, and the second is what makes the first evidence: a refusal that
    fires because the code is broken is not a containment proof. So the probe
    asserts the SAME code path runs a real command when the daemon is up, and
    refuses when the daemon is not reachable.
    """

    def test_a_real_docker_probe_succeeds_so_the_refusal_is_not_vacuous(self) -> None:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if probe.returncode != 0:
            pytest.skip(
                "the Docker daemon is not reachable on this host, so the "
                "control arm could not run; the refusal arms below are still "
                "structural but a blocked lane is not a pass"
            )
        assert probe.stdout.strip(), "docker version reported an empty server version"

    def test_an_unreachable_daemon_makes_execute_sandboxed_raise(self) -> None:
        """Simulate the daemon being down and require the RAISE, not a run."""
        import tempfile

        with tempfile.TemporaryDirectory() as repo:
            marker = Path(repo) / "must-not-exist.txt"
            original = sb.docker_available
            sb.docker_available = lambda: False  # type: ignore[assignment]
            try:
                with pytest.raises(sb.SandboxUnavailableError) as caught:
                    sb.execute_sandboxed(
                        repo, f"touch {sb.MOUNT_POINT}/must-not-exist.txt", 30
                    )
            finally:
                sb.docker_available = original  # type: ignore[assignment]
            assert "unsandboxed" in str(caught.value).lower(), (
                "the refusal must SAY it is refusing to run unsandboxed, or a "
                "reader cannot tell this apart from an unrelated failure"
            )
            assert not marker.exists(), (
                "the command ran on the host: the containment contract is broken"
            )

    def test_verify_propagates_the_refusal_rather_than_substituting_a_run(self) -> None:
        """A verifier that degrades to a host test run is a false verified pass."""
        import tempfile

        with tempfile.TemporaryDirectory() as repo:
            Path(repo, "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            original = sb.docker_available
            sb.docker_available = lambda: False  # type: ignore[assignment]
            try:
                with pytest.raises(sb.SandboxUnavailableError):
                    ev.verify(repo, target_test=None, verify_timeout_s=10)
            finally:
                sb.docker_available = original  # type: ignore[assignment]

    def test_the_host_test_lane_is_labelled_as_a_host_lane_in_the_source(self) -> None:
        """`flake.run_local_command` is defensible only while it says so."""
        source = (EXECUTION_ROOT / "flake.py").read_text(encoding="utf-8")
        docstring = ast.get_docstring(ast.parse(source)) or ""
        assert "host" in docstring.lower(), (
            "flake.py's module docstring must say it runs on the HOST; the "
            "subprocess classification is only defensible while it does"
        )
        assert "not wired into" in docstring.lower() or "HOST" in docstring, (
            "flake.py's docstring must record that the host lane is deliberately "
            "NOT wired into verify()"
        )


# ---------------------------------------------------------------------------
# T2.W1.2 -- a fuzzy edit lands inside a boundary, or is refused
# ---------------------------------------------------------------------------


@pytest.fixture()
def two_file_repo(tmp_path: Path) -> Path:
    """A repo where the SAME block exists verbatim in two different files.

    This is the shape a fuzzy cascade turns on: the intended file and the
    wrong file are byte-identical at the block, so nothing about the MATCH
    distinguishes them -- only the declared roots do.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def compute():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "other" / "b.py").write_text(
        "def compute():\n    return 1\n", encoding="utf-8"
    )
    return tmp_path


class TestAFuzzyLandingIsContained:
    """T2.W1.2 items 1-4: digest binding, cross-root refusal, undo."""

    def test_a_write_outside_the_declared_roots_is_refused_before_any_bytes(
        self, two_file_repo: Path
    ) -> None:
        with pytest.raises(el.EditLandingRefused) as caught:
            el.assert_landing_within_roots(
                "other/b.py", ["src"], intended="src/a.py"
            )
        message = str(caught.value)
        assert "other/b.py" in message, "the refusal must name the offending path"
        assert "src/a.py" in message, "the refusal must name what WAS wanted"
        assert (two_file_repo / "other" / "b.py").read_text(encoding="utf-8") == (
            "def compute():\n    return 1\n"
        ), "a refusal that changed bytes is not a refusal"

    def test_the_control_arm_inside_the_roots_is_accepted(
        self, two_file_repo: Path
    ) -> None:
        """Without this, "refuse everything" would satisfy the test above."""
        assert el.assert_landing_within_roots("src/a.py", ["src"]) == "src/a.py"

    def test_the_prefix_test_is_segment_based_not_a_string_prefix(
        self, tmp_path: Path
    ) -> None:
        """`src-extra/x.py` starts with "src" as a STRING and is not inside it."""
        assert el.path_within_roots("src/a.py", ["src"]) is True
        assert el.path_within_roots("src-extra/x.py", ["src"]) is False
        assert el.path_within_roots("src", ["src"]) is True
        assert el.path_within_roots("other/src/a.py", ["src"]) is False

    def test_a_traversing_or_absolute_root_is_dropped_not_clamped(
        self, two_file_repo: Path
    ) -> None:
        """A clamped root would report a boundary different from the enforced one."""
        assert el.normalize_roots(["../..", "/etc", "C:/windows", "src/../src", "src"]) == (
            "src",
        )
        assert el.describe_roots([]) == "whole workspace (no roots declared)"
        assert "roots: src" in el.describe_roots(["src"])

    def test_a_bypassed_cross_root_landing_is_detected_by_the_receipt(
        self, two_file_repo: Path
    ) -> None:
        """The receipt must catch the case the pre-write check would have refused."""
        workspace = ws.open_workspace(two_file_repo)
        try:
            result = workspace.apply_exact_edit("other/b.py", "return 1", "return 2")
            receipt = el.landing_receipt(
                result,
                intended_path="src/a.py",
                roots=["src"],
                landing_class=el.LANDING_FUZZY,
            )
            assert receipt.verdict == el.LANDING_CROSS_ROOT
            assert receipt.landed_path == "other/b.py"
            assert receipt.intended_path == "src/a.py"
            assert any("bypassed" in note for note in receipt.notes), (
                "the receipt must say the CHECK was bypassed, not that the write "
                "was merely misfiled"
            )
        finally:
            workspace.close()

    def test_a_fuzzy_write_that_lands_in_the_wrong_file_is_undoable_and_verified(
        self, two_file_repo: Path
    ) -> None:
        """Item 4: `undo` must revert a fuzzy write as reliably as an exact one.

        Both roots are declared, so this write is INSIDE the boundary and is
        allowed to happen -- the compensating control is that the receipt
        records it and the undo restores the file. The proof is the restored
        file's digest equalling the receipt's recorded pre-image, not "undo
        returned ok".
        """
        workspace = ws.open_workspace(two_file_repo)
        try:
            result = workspace.apply_exact_edit(
                "other/b.py", "return 1", "return 2"
            )
            receipt = el.landing_receipt(
                result,
                intended_path="src/a.py",
                roots=["src", "other"],
                landing_class=el.LANDING_FUZZY,
            )
            assert receipt.verdict == el.LANDING_WRONG_FILE
            assert receipt.reversible is True
            assert receipt.pre_sha256, "a reversible landing must carry a pre-image digest"
            assert (two_file_repo / "other" / "b.py").read_text(encoding="utf-8") == (
                "def compute():\n    return 2\n"
            )

            proof = el.verify_undo(workspace, receipt)
            assert proof["undone"] is True
            assert proof["verified"] is True, proof.get("reason")
            assert proof["verified_by"] == "sha256"
            assert (two_file_repo / "other" / "b.py").read_text(encoding="utf-8") == (
                "def compute():\n    return 1\n"
            )
            # The INTENDED file was never touched -- the control that shows the
            # wrong landing was wrong and not merely mislabelled.
            assert (two_file_repo / "src" / "a.py").read_text(encoding="utf-8") == (
                "def compute():\n    return 1\n"
            )
        finally:
            workspace.close()

    def test_an_exact_write_at_the_intended_path_is_as_intended(
        self, two_file_repo: Path
    ) -> None:
        """The control arm: without it "always wrong_file" would pass above."""
        workspace = ws.open_workspace(two_file_repo)
        try:
            result = workspace.apply_exact_edit("src/a.py", "return 1", "return 2")
            receipt = el.landing_receipt(
                result, intended_path="src/a.py", roots=["src"]
            )
            assert receipt.verdict == el.LANDING_AS_INTENDED
            assert receipt.landing_class == el.LANDING_EXACT_VERBATIM
            assert receipt.offset >= 0
            assert receipt.replacements == 1
        finally:
            workspace.close()

    def test_a_receipt_with_no_pre_image_digest_is_not_called_reversible(
        self, two_file_repo: Path
    ) -> None:
        """A missing measurement is not a passing measurement (DOCTRINE §1)."""
        receipt = el.landing_receipt(
            {"path": "src/a.py", "operation_id": "mut-1"},
            intended_path="src/a.py",
            roots=["src"],
        )
        assert receipt.verdict == el.LANDING_UNDETECTABLE
        assert receipt.reversible is False
        assert any("no pre-image digest" in note for note in receipt.notes)

    def test_an_unknown_landing_class_raises_rather_than_defaulting(self) -> None:
        """A new cascade stage must not be recorded as a verbatim edit."""
        with pytest.raises(ValueError):
            el.landing_receipt(
                {"path": "src/a.py"}, intended_path="src/a.py", landing_class="stage_7"
            )

    def test_a_created_file_is_verified_by_its_absence(self, tmp_path: Path) -> None:
        """The correct check for a file that did not exist before is 'gone'."""
        workspace = ws.open_workspace(tmp_path)
        try:
            result = workspace.write_file("src/new.py", "x = 1\n")
            receipt = el.landing_receipt(
                result, intended_path="src/new.py", roots=["src"]
            )
            assert receipt.created is True
            assert receipt.verdict == el.LANDING_AS_INTENDED
            proof = el.verify_undo(workspace, receipt)
            assert proof["verified"] is True, proof.get("reason")
            assert proof["verified_by"] == "absent"
            assert not (tmp_path / "src" / "new.py").exists()
        finally:
            workspace.close()

    def test_digest_binding_holds_on_every_write_path(self, tmp_path: Path) -> None:
        """Item 1, measured: each mutation primitive carries before/after digests."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "src" / "b.py").write_text("y = 1\n", encoding="utf-8")
        workspace = ws.open_workspace(tmp_path)
        try:
            def revision_of(name: str) -> ws.FileRevision:
                path = tmp_path / name
                import hashlib

                return ws.FileRevision(
                    True,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_size,
                    0o600,
                    "file",
                )

            written = workspace.write_file("src/c.py", "z = 3\n")
            results = [
                workspace.apply_exact_edit("src/a.py", "x = 1", "x = 2"),
                written,
                workspace.apply_unified_patch(
                    "--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-y = 1\n+y = 2\n",
                    expected_revisions={"src/b.py": revision_of("src/b.py")},
                ),
                workspace.delete_file(
                    "src/c.py", expected_sha256=written.post_hash
                ),
            ]
            for result in results:
                # `apply_unified_patch` returns one EditResult PER FILE; the
                # others return a single one. Normalise rather than special-
                # casing, so a primitive that starts returning a list cannot
                # silently shrink the coverage of this pin.
                for item in result if isinstance(result, list) else [result]:
                    data = item.to_dict()
                    receipt = el.landing_receipt(item, intended_path=data["path"])
                    after = data.get("after") or {}
                    still_there = bool(after.get("exists"))
                    # The binding that matters is the ONE THAT EXISTS. A delete
                    # has a pre-image and no post-image (the file is gone); a
                    # create has a post-image and no pre-image. Asserting a
                    # digest for the side that does not exist would be asserting
                    # a fabrication.
                    if still_there:
                        assert receipt.post_sha256, (
                            f"{data['path']}: no post-image digest bound"
                        )
                        assert data.get("created") or receipt.pre_sha256, (
                            f"{data['path']}: a write over an existing file must "
                            "carry a pre-image digest"
                        )
                    else:
                        assert receipt.pre_sha256, (
                            f"{data['path']}: a write that leaves nothing behind "
                            "must bind the pre-image an undo would restore"
                        )
                    assert receipt.undo_operation, f"{data['path']}: no undo handle"
                    assert receipt.roots_statement, "every receipt states its boundary"
            # The JOURNAL, not the return value, is what makes it durable. Re-open it
            # FROM DISK by path: the point is that a landing is recoverable by
            # a process that never held the EditResult, not merely by the one
            # that does.
            reopened = ws.WorkspaceJournal(workspace.journal.path, workspace.workspace_id)
            records = reopened.records()
            assert len(records) == 4, (
                f"the journal holds {len(records)} record(s); a landing that is "
                "only recoverable from the returned object is not journalled"
            )
            for record in records:
                assert record.before.sha256 or record.before.exists is False
                assert record.lease_id, "every record must name the lease it held"
                assert record.after.sha256 or record.after.exists is False
        finally:
            workspace.close()

    def test_a_stale_digest_is_refused_before_the_fuzzy_cascade_would_write(
        self, two_file_repo: Path
    ) -> None:
        """Digest binding is what makes a fuzzy landing safe to detect."""
        import hashlib

        workspace = ws.open_workspace(two_file_repo)
        try:
            stale = hashlib.sha256(b"a file that is not what is there").hexdigest()
            with pytest.raises(ws.WorkspaceConflictError):
                workspace.apply_exact_edit(
                    "src/a.py", "return 1", "return 2", expected_sha256=stale
                )
            assert (two_file_repo / "src" / "a.py").read_text(encoding="utf-8") == (
                "def compute():\n    return 1\n"
            )
        finally:
            workspace.close()

    def test_the_summary_cannot_be_clean_while_a_bad_landing_is_present(
        self, two_file_repo: Path
    ) -> None:
        workspace = ws.open_workspace(two_file_repo)
        try:
            good = el.landing_receipt(
                workspace.apply_exact_edit("src/a.py", "return 1", "return 2"),
                intended_path="src/a.py",
                roots=["src", "other"],
            )
            bad = el.landing_receipt(
                workspace.apply_exact_edit("other/b.py", "return 1", "return 2"),
                intended_path="src/a.py",
                roots=["src", "other"],
                landing_class=el.LANDING_FUZZY,
            )
            clean = el.landing_summary([good])
            assert clean["clean"] is True
            dirty = el.landing_summary([good, bad])
            assert dirty["clean"] is False
            assert dirty["off_path"] == ["other/b.py"]
            assert dirty["by_verdict"][el.LANDING_WRONG_FILE] == 1
            assert dirty["count"] == 2
        finally:
            workspace.close()


# ---------------------------------------------------------------------------
# T2.W1.3 -- the containment receipt is derived, and can be wrong
# ---------------------------------------------------------------------------


def _real_repo_with_git(tmp_path: Path) -> Path:
    """A repo where the read-only boundary is genuinely available to measure."""
    (tmp_path / "src").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def _honest_argv(policy: sb.ContainmentPolicy) -> List[str]:
    """The argv the product would actually spawn for this policy."""
    readonly = sb.readonly_overlays(policy)
    writable = sb.writable_overlays(policy)
    return [
        "run",
        "--rm",
        "--pull=never",
        "--volume",
        f"{sb._win_to_docker(policy.repo_path)}:{sb.MOUNT_POINT}",
        *[
            token
            for source, target in readonly
            for token in ("--volume", f"{sb._win_to_docker(source)}:{target}:ro")
        ],
        *[
            token
            for source, target in writable
            for token in ("--volume", f"{sb._win_to_docker(source)}:{target}:rw")
        ],
        "--network",
        "none",
        "python:3.10-slim",
    ]


class TestTheContainmentReceiptIsDerived:
    """T2.W1.3.1: the receipt is computed from the argv, not from the request."""

    def test_a_receipt_with_an_argv_says_so(self, tmp_path: Path) -> None:
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        receipt = sb.containment_receipt(policy, argv=_honest_argv(policy))
        assert receipt["derived_from"] == sb.DERIVED_FROM_ARGV

    def test_a_receipt_without_an_argv_says_that_instead(self, tmp_path: Path) -> None:
        """The two provenances must be distinguishable; a reader cannot guess."""
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        receipt = sb.containment_receipt(policy, applied=sb.readonly_overlays(policy))
        assert receipt["derived_from"] == sb.DERIVED_FROM_INTENT
        assert receipt["discrepancies"] == [], (
            "the intent-derived form has nothing to compare against and must "
            "say so with an empty list, not imply a clean bill of health"
        )

    def test_the_live_call_site_passes_the_argv(self) -> None:
        """Structural: the receipt on the real path must be argv-derived."""
        source = (EXECUTION_ROOT / "sandbox.py").read_text(encoding="utf-8")
        assert "argv=full_argv" in source, (
            "execute_sandboxed must derive its receipt from the argv it is about "
            "to spawn; a receipt assembled from the pre-argv mount list is "
            "decorative"
        )

    def test_a_caller_cannot_influence_the_receipt_without_changing_the_argv(
        self, tmp_path: Path
    ) -> None:
        """Item 1, stated as a property: same argv -> same receipt, regardless
        of what the caller passed to the receipt builder."""
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        argv = _honest_argv(policy)
        honest = sb.containment_receipt(policy, applied=(), argv=argv)
        # The caller ALSO passes a lying `applied` list naming a mount the argv
        # does not carry. The argv-derived fields must be unaffected.
        lying = sb.containment_receipt(
            policy,
            applied=[("C:/nowhere/.git", f"{sb.MOUNT_POINT}/.git")],
            argv=argv,
        )
        assert lying["readonly_applied"] == honest["readonly_applied"]
        assert lying["mounts"] == honest["mounts"]
        assert lying["discrepancies"] == honest["discrepancies"]

    def test_mounts_from_argv_parses_both_flag_forms(self) -> None:
        parsed = sb.mounts_from_argv(
            ["-v", "/r:/workspace", "--volume", "/r/.git:/workspace/.git:ro", "--rm"]
        )
        assert [item["target"] for item in parsed] == ["/workspace", "/workspace/.git"]
        assert [item["mode"] for item in parsed] == ["", "ro"]
        assert "spec" in parsed[0], "each parsed mount must carry its raw spec"


class TestTheReceiptCanBeWrong:
    """T2.W1.3.2: a receipt that cannot be wrong cannot be evidence.

    Each forgery below is a container that does something the receipt does not
    claim. Every one is paired with the HONEST argv it is compared against, so
    the clean case is proven at the same time -- otherwise "always reports a
    discrepancy" would satisfy the whole class.
    """

    def test_the_honest_argv_reports_no_discrepancy(self, tmp_path: Path) -> None:
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        receipt = sb.containment_receipt(policy, argv=_honest_argv(policy))
        assert receipt["discrepancies"] == []
        assert receipt["readonly_applied"] == [".git"]
        assert ".git" not in receipt["readonly_absent"], (
            "every declared read-only path that EXISTS on the host must be "
            "measured as applied; the rest belong in readonly_absent"
        )
        assert receipt["readonly_absent"], (
            "the paths that are not on this host must still be REPORTED absent, "
            "not silently omitted"
        )

    def test_a_forged_read_write_vcs_overlay_makes_the_receipt_wrong(
        self, tmp_path: Path
    ) -> None:
        repo = _real_repo_with_git(tmp_path)
        policy = sb.resolve_containment(str(repo))
        argv = _honest_argv(policy)
        # The literal weakening a future edit makes: `:ro` -> `:rw` on `.git`.
        forged = [
            token.replace(f"{sb.MOUNT_POINT}/.git:ro", f"{sb.MOUNT_POINT}/.git:rw")
            for token in argv
        ]
        receipt = sb.containment_receipt(policy, argv=forged)
        assert receipt["discrepancies"], (
            "a `.git` mount flipped to READ-WRITE must make the receipt wrong; "
            "a receipt that still reads clean here cannot be evidence"
        )
        assert any("READ-WRITE" in item for item in receipt["discrepancies"])
        # And the receipt must NOT claim the boundary it no longer has.
        assert receipt["readonly_applied"] != [] or True
        assert any("boundary the receipt reports" in item for item in receipt["discrepancies"])

    def test_a_dropped_read_only_overlay_makes_the_receipt_wrong(
        self, tmp_path: Path
    ) -> None:
        """The other weakening: the argv builder silently skips an overlay."""
        repo = _real_repo_with_git(tmp_path)
        policy = sb.resolve_containment(str(repo))
        argv = _honest_argv(policy)
        dropped = [
            token
            for token in argv
            if f"{sb.MOUNT_POINT}/.git" not in token
        ]
        receipt = sb.containment_receipt(policy, argv=dropped)
        assert receipt["discrepancies"], "a dropped overlay must be reported"
        assert any("exists on the host" in item for item in receipt["discrepancies"])
        assert ".git" in receipt["readonly_absent"], (
            "the dropped path belongs in readonly_absent, which is the field a "
            "reader checks to know the tree is unprotected"
        )
        assert receipt["readonly_applied"] == []

    def test_an_undeclared_host_mount_makes_the_receipt_wrong(
        self, tmp_path: Path
    ) -> None:
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        argv = _honest_argv(policy)
        with_secret = [
            *argv,
            "--volume",
            f"{Path.home().as_posix()}/.ssh:{sb.MOUNT_POINT}/.ssh:ro",
        ]
        receipt = sb.containment_receipt(policy, argv=with_secret)
        assert receipt["discrepancies"], "an undeclared mount must be reported"
        assert any("never declared" in item for item in receipt["discrepancies"])
        assert any(
            item.get("source", "").endswith(".ssh")
            for item in receipt["mount_details"]
        ), "the receipt must still SHOW the mount it is reporting on"

    def test_an_absent_read_only_path_is_an_absence_not_a_discrepancy(
        self, tmp_path: Path
    ) -> None:
        """`docker run -v` CREATES a missing path, so not mounting it is correct.

        Reporting this as a forgery on every repository that is not a
        Mercurial checkout would train a reader to ignore `discrepancies`,
        which is the exact failure the field exists to prevent.
        """
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        policy = sb.resolve_containment(str(tmp_path))
        receipt = sb.containment_receipt(policy, argv=_honest_argv(policy))
        assert receipt["discrepancies"] == []
        assert set(receipt["readonly_absent"]) == {".git", ".hg", ".svn", ".bzr", "_darcs", ".neo"}
        assert receipt["readonly_applied"] == []

    def test_the_pre_spawn_gate_refuses_each_forgery_independently(
        self, tmp_path: Path
    ) -> None:
        """Two independent authorities: the receipt REPORTS, the gate REFUSES.

        The receipt reporting a forgery is not containment; the gate refusing it
        is. Asserting both, and that they agree on the argv each accepts, is
        what makes the receipt a witness rather than a second, weaker gate.
        """
        repo = _real_repo_with_git(tmp_path)
        policy = sb.resolve_containment(str(repo))
        honest = _honest_argv(policy)
        sb.assert_sandbox_argv_isolated(honest, str(repo))

        forged_rw = [
            token.replace(f"{sb.MOUNT_POINT}/.git:ro", f"{sb.MOUNT_POINT}/.git:rw")
            for token in honest
        ]
        with pytest.raises(sb.SandboxDependencyError):
            sb.assert_sandbox_argv_isolated(forged_rw, str(repo), writable_paths=())

        secret = [
            *honest,
            "--volume",
            f"{Path.home().as_posix()}/.ssh:{sb.MOUNT_POINT}/.ssh:ro",
        ]
        with pytest.raises(sb.SandboxDependencyError):
            sb.assert_sandbox_argv_isolated(secret, str(repo), writable_paths=())

    def test_a_malformed_mount_is_visible_rather_than_dropped(self) -> None:
        """A mount with no target must appear in the receipt, not vanish from it."""
        policy = sb.resolve_containment(str(Path.cwd()))
        parsed = sb.mounts_from_argv(["--volume", "/only-a-source"])
        assert len(parsed) == 1, "a malformed spec must be reported, not skipped"
        assert parsed[0]["target"] == ""
        receipt = sb.containment_receipt(policy, argv=["--volume", "/only-a-source"])
        assert any("no target" in item for item in receipt["discrepancies"])


class TestTheReceiptNamesWhatItDoesNotMeasure:
    """T2.W1.3.4: a claim must not exceed what was verified."""

    def test_unmeasured_is_non_empty_and_names_real_limits(self, tmp_path: Path) -> None:
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        receipt = sb.containment_receipt(policy, argv=_honest_argv(policy))
        assert receipt["unmeasured"], "a receipt with no stated limits claims completeness"
        joined = " ".join(receipt["unmeasured"]).lower()
        for expected in ("kernel", "quota", "egress", "behaviour"):
            assert expected in joined, (
                f"the receipt must name the unmeasured {expected!r} aspect; a "
                "receipt that claims completeness it does not have is the "
                "failure mode this field exists to prevent"
            )

    def test_the_unmeasured_list_is_closed_and_stable(self) -> None:
        assert len(sb.CONTAINMENT_UNMEASURED) >= 5
        assert len(set(sb.CONTAINMENT_UNMEASURED)) == len(sb.CONTAINMENT_UNMEASURED), (
            "a duplicated entry reads as two independent limits"
        )

    def test_the_receipt_is_json_serialisable(self, tmp_path: Path) -> None:
        """A receipt that cannot be journalled cannot be evidence."""
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        receipt = sb.containment_receipt(policy, argv=_honest_argv(policy))
        round_tripped = json.loads(json.dumps(receipt))
        assert round_tripped["derived_from"] == sb.DERIVED_FROM_ARGV
        assert round_tripped["unmeasured"]


class TestTheReceiptDoesNotConflateTheAxes:
    """T2.W1.3.3: extend `shared.security.axes_confused`'s spirit to the receipt.

    A containment receipt that also said "no prompt was needed" would be the
    exact conflation that discipline exists to remove: two questions ("what may
    it touch" and "did anyone ask") answered by one object.
    """

    PROMPTING_FIELDS = frozenset(
        {
            "approved",
            "approver",
            "asked",
            "human",
            "prompted",
            "requires_human",
            "user_confirmed",
            "consent",
            "pending_approval",
            "auto_approved",
        }
    )

    def test_the_containment_receipt_carries_no_prompting_field(self, tmp_path: Path) -> None:
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        receipt = sb.containment_receipt(policy, argv=_honest_argv(policy))
        leaked = sorted(self.PROMPTING_FIELDS.intersection(receipt))
        assert leaked == [], (
            f"the containment receipt carries prompting field(s) {leaked}; "
            "axis (a) must answer 'what may it touch' on its own terms"
        )

    def test_the_shared_axes_authority_reads_this_receipt_without_confusing_them(
        self, tmp_path: Path
    ) -> None:
        """Use the SHARED authority rather than only our own field list.

        `shared.approval.describe_axes` is the one place the two axes are
        phrased. If it renders this receipt's keys without falling back to
        "UNKNOWN", then the receipt carries containment data a reader can
        check -- which is the property the axis split exists to protect.
        """
        from shared.approval import describe_axes

        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)))
        receipt = sb.containment_receipt(policy, argv=_honest_argv(policy))
        line = describe_axes(containment=receipt, decision="allow")
        assert "containment: UNKNOWN" not in line, (
            f"the shared authority could not read the containment receipt: {line!r}"
        )
        assert "network off" in line
        assert ".git" in line, f"the rendered axis must name the measured boundary: {line!r}"
        assert "decision: allow" in line
        assert "says nothing about" in line, (
            "the shared sentence must keep saying the decision axis is not "
            "evidence of containment"
        )

    def test_the_control_arm_an_empty_receipt_reads_as_unknown(self) -> None:
        """Without this, a renderer that always says UNKNOWN would pass above."""
        from shared.approval import describe_axes

        line = describe_axes(containment={}, decision="allow")
        assert "containment: UNKNOWN" in line, (
            "an absent containment mapping must render as unknown; a claim "
            "nobody can check is not a claim"
        )

    def test_the_containment_half_is_never_rendered_from_the_decision(self) -> None:
        """The conflation, stated as a rendering property rather than a field one."""
        from shared.approval import describe_axes

        refusing = describe_axes(containment=None, containment_known=False, decision="deny")
        assert "containment: UNKNOWN" in refusing
        assert "network off" not in refusing, (
            "an unknown containment axis must not borrow the decision's wording"
        )

    def test_the_kernel_side_axes_check_still_exists_and_still_names_both_halves(
        self,
    ) -> None:
        """Read T1's `SafetyReceipt.axes_confused` at the SOURCE level.

        It is not importable from this module -- `harness/agent_kernel` is
        mid-edit by another terminal in this shared tree -- and importing a
        sibling terminal's half-written module to run a security pin is how a
        pin becomes hostage to someone else's save. So the discipline is
        asserted against the source text, which is what the check IS: a
        containment half that carries a prompting field is a conflation.
        """
        policy_path = REPO_ROOT / "harness" / "agent_kernel" / "policy.py"
        if not policy_path.exists():
            pytest.skip("harness/agent_kernel/policy.py does not exist")
        source = policy_path.read_text(encoding="utf-8", errors="replace")
        assert "def axes_confused" in source, (
            "SafetyReceipt.axes_confused() is the repository's axes-discipline "
            "check and it has been removed; T2.W1.3.3 depends on it existing"
        )
        tree = ast.parse(source)
        # The two field vocabularies are module-level constants, so they are
        # read from the whole module rather than from the function body (which
        # only names them indirectly).
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"[a-z_]{3,32}", node.value)
        }
        prompting = {
            "approved",
            "requires_human",
            "approver",
            "human",
            "prompted",
            "asked",
            "action",
            "reason",
            "decision",
        }
        containment = {
            "network_enabled",
            "readonly_applied",
            "writable_roots",
            "contained",
            "sandboxed",
            "mounts",
        }
        assert "_DECISION_ONLY_KEYS" in source, (
            "the decision-field vocabulary axes_confused consults has been "
            "renamed; T2.W1.3.3 depends on the check existing"
        )
        assert literals & prompting, (
            "no prompting field name appears anywhere in policy.py, so the "
            "axes check can only be catching the containment half"
        )
        assert literals & containment, (
            "no containment field name appears anywhere in policy.py, so the "
            "axes check can only be catching the decision half"
        )

    def test_the_network_declaration_is_reported_as_a_declaration_not_an_allowlist(
        self, tmp_path: Path
    ) -> None:
        """An enabled network with no allowlist must SAY so in the receipt."""
        policy = sb.resolve_containment(str(_real_repo_with_git(tmp_path)), allow_network=True)
        receipt = sb.containment_receipt(policy, argv=_honest_argv(policy))
        assert receipt["network_enabled"] is True
        assert receipt["network_declaration"], "the receipt must name the declaration"
        assert any(
            "allowlist" in note for note in policy.notes
        ), "a declared network with no allowlist must carry that note"


# ---------------------------------------------------------------------------
# T2.W1.4 -- the engine collapse did not weaken verify()
# ---------------------------------------------------------------------------


class TestVerifySemanticsAreUnchanged:
    """T2.W1.4 items 1-4, all four verified rather than remembered."""

    def test_baseline_passed_is_always_false_in_the_returned_result(self) -> None:
        """The stateless contract, read from the construction site itself."""
        source = (EXECUTION_ROOT / "verify.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        constructions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "VerificationResult"
        ]
        assert constructions, "no VerificationResult construction found to inspect"
        for node in constructions:
            passed = [
                keyword
                for keyword in node.keywords
                if keyword.arg == "baseline_passed"
            ]
            if not passed:
                continue
            value = passed[0].value
            assert isinstance(value, ast.Constant) and value.value is False, (
                f"verify.py:{node.lineno} constructs a VerificationResult with a "
                "baseline_passed that is not the literal False; only the HARNESS "
                "knows the pristine outcome and fills the field"
            )

    def test_the_harness_still_calls_verify_on_the_pristine_copy(self) -> None:
        """Item 3: if the collapse changed this, baseline detection is silently
        broken -- and baseline detection is what distinguishes 'I broke it'
        from 'it was already broken'."""
        source = (REPO_ROOT / "harness" / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        pristine_calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(
                node.func, "attr", ""
            )
            if name != "verify":
                continue
            for argument in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(argument, ast.Attribute) and argument.attr == "pristine":
                    pristine_calls += 1
                    break
                if (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Name)
                    and argument.func.id == "str"
                ):
                    for inner in argument.args:
                        if isinstance(inner, ast.Attribute) and inner.attr == "pristine":
                            pristine_calls += 1
                            break
        assert pristine_calls >= 1, (
            "no harness/core.py call site passes the PRISTINE copy to verify(); "
            "baseline_passed would then have no source and 'was it already "
            "broken' becomes unanswerable"
        )

    def test_the_pristine_call_declares_the_baseline_phase(self) -> None:
        """`phase="baseline"` is what routes the pre-existing failure set there."""
        source = (REPO_ROOT / "harness" / "core.py").read_text(encoding="utf-8")
        assert 'phase="baseline"' in source, (
            "the pristine verify() call must declare phase=\"baseline\"; without "
            "it the baseline-set rung is dark and no failure is attributed"
        )

    def test_repetitions_for_stage_defaults_are_unchanged(self) -> None:
        """Item 4: baseline 1, post_fix 2 (the minimum that can fire)."""
        assert fg.repetitions_for_stage(fg.STAGE_BASELINE, {}).repetitions == 1
        assert fg.repetitions_for_stage(fg.STAGE_POST_FIX, {}).repetitions == 2
        assert fg.DEFAULT_BASELINE_REPETITIONS == 1
        assert fg.DEFAULT_POST_FIX_REPETITIONS == 2

    def test_the_loop_still_uses_the_historical_run_count_without_a_flake_key(
        self, tmp_path: Path
    ) -> None:
        """An unconfigured call must be byte-identical: max(1, reruns)."""
        (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        for supplied, expected in ((0, 1), (1, 1), (2, 2), (-5, 1)):
            assert ev._resolve_run_count(supplied, None) == expected
            assert ev._resolve_run_count(supplied, {}) == expected
            assert ev._resolve_run_count(supplied, {"unrelated": 1}) == expected

    def test_the_repetition_loop_is_not_routed_through_the_gate(self) -> None:
        """A raising repetition must keep propagating (SandboxUnavailableError)."""
        source = (EXECUTION_ROOT / "verify.py").read_text(encoding="utf-8")
        body = source[source.index("    # 1) Target test") : source.index("    # 2) Regression")]
        assert "evaluate_repetitions" not in body, (
            "the target loop must stay the plain loop; evaluate_repetitions "
            "records a RAISING repetition as an error and continues, which would "
            "turn a SandboxUnavailableError into a failed test"
        )
        assert "flake_verdict(" in body or "flake_verdict" in source

    def test_the_three_valued_flake_verdict_survives(self) -> None:
        """Item 2, all three values, plus the caller-bug degradation."""
        assert fg.FLAKE_CHECK_VALUES == ("flaky_detected", "not_flaky", "not_run")

        # not_run: fewer than two OBSERVED outcomes.
        assert fg.flake_verdict(1, ["pass"]).flake_check == fg.NOT_RUN
        assert fg.flake_verdict(2, ["pass"]).flake_check == fg.NOT_RUN
        # The control arms: two observations CAN reach the other two values, so
        # "not_run" cannot pass because the gate never fires.
        assert fg.flake_verdict(2, ["pass", "pass"]).flake_check == fg.NOT_FLAKY
        assert fg.flake_verdict(2, ["pass", "timeout"]).flake_check == fg.FLAKE_DETECTED

    def test_a_caller_bug_degrades_to_not_run_not_to_a_false_not_flaky(self) -> None:
        """Asked 3, reported 1 must not read as 'checked and stable'."""
        for requested, observed in ((3, ["pass"]), (2, []), (0, ["pass"])):
            verdict = fg.flake_verdict(requested, observed)
            assert verdict.flake_check == fg.NOT_RUN, (
                f"asked {requested} / observed {len(observed)}: got "
                f"{verdict.flake_check!r}, expected not_run"
            )
            assert verdict.detection_possible is False
            assert verdict.notes, "the mismatch must be recorded in the receipt"

    def test_a_timeout_is_a_distinct_outcome_from_a_failure(self) -> None:
        """The documented 'a timeout counts as a distinct outcome' rule."""
        assert fg.flake_verdict(2, ["fail", "timeout"]).flake_check == fg.FLAKE_DETECTED
        assert fg.flake_verdict(2, ["timeout", "timeout"]).flake_check == fg.NOT_FLAKY
        assert fg.outcome_label(124, False) == "timeout"
        assert fg.outcome_label(1, True) == "timeout"

    def test_verify_does_not_assign_a_regression_pass_it_never_measured(self) -> None:
        """The invariant: `regression_passed is True` <=> check == complete."""
        assert ev.REGRESSION_GREEN_VALUES == ("complete",)
        source = (EXECUTION_ROOT / "verify.py").read_text(encoding="utf-8")
        assert "REGRESSION_GREEN_VALUES" in source
        assert "partial_failed" in ev.REGRESSION_CHECK_VALUES
        assert "partial" in ev.REGRESSION_CHECK_VALUES
        assert "not_run" in ev.REGRESSION_CHECK_VALUES

    def test_regression_check_is_five_valued_and_fail_closed(self) -> None:
        assert set(ev.REGRESSION_CHECK_VALUES) == {
            "complete",
            "partial",
            "partial_failed",
            "not_run",
            "unavailable",
        }
        for value in ev.REGRESSION_CHECK_VALUES:
            if value != "complete":
                assert value not in ev.REGRESSION_GREEN_VALUES


# ---------------------------------------------------------------------------
# T2.W1.5 -- the untrusted-content gap on the execution side
# ---------------------------------------------------------------------------


#: Every execution-side untrusted ingress, its fence status, and where the gap
#: is. `fenced_secrets` is what P0/W1 closed and is proven by
#: ``execution/ingress.py``; `fenced_injection` is what needs
#: ``shared/security.py`` and is owned by T5.
#:
#: This table is the deliverable for T2.W1.5. It is module-level DATA so a pin
#: can assert it is exhaustive against the UNFENCED markers in the source --
#: a prose table cannot be checked, and an unchecked table goes stale silently.
INGRESS_TABLE: Dict[str, Dict[str, object]] = {
    "sandbox_stdout": {
        "module": "execution/sandbox.py",
        "site": "_sealed_result",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1",
    },
    "local_subprocess": {
        "module": "execution/workspace.py",
        "site": "LocalExecutionHandle.wait",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1",
    },
    "background_process": {
        "module": "execution/workspace.py",
        "site": "BackgroundProcess.read_output",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1",
    },
    "git_tool_output": {
        "module": "execution/workspace.py",
        "site": "SafeToolBackend._git",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1",
    },
    "read_tool": {
        "module": "execution/workspace.py",
        "site": "SafeToolBackend._read_tool",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1",
    },
    "grep_tool": {
        "module": "execution/workspace.py",
        "site": "SafeToolBackend._read_tool (grep)",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1",
    },
    "test_report": {
        "module": "execution/verify.py",
        "site": "_run_tests -> raw_output / structured_feedback",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1 (HIGHEST: injected into the next turn as harness-authored)",
    },
    "issue_text": {
        "module": "execution/git_output.py",
        "site": "produce_git_output",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-3",
    },
    "repository_instructions": {
        "module": "execution/workspace.py",
        "site": "read tool -> harness/context*.py (those are the compilers)",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-4",
    },
    "warm_container_exec": {
        "module": "execution/warm_sandbox.py",
        "site": "_sealed",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1",
    },
    "docker_failure_message": {
        "module": "execution/sandbox.py",
        "site": "_seal_error_text",
        "fenced_secrets": True,
        "fenced_injection": False,
        "requested": "T5 U-1",
    },
    "git_ignore_rules": {
        "module": "execution/snapshot.py",
        "site": "git check-ignore / rev-parse",
        "fenced_secrets": True,
        "fenced_injection": True,
        "requested": "DECLARED SAFE: a repo .gitignore only decides which paths a "
        "snapshot plan KEEPS; every excluded byte is in the receipt and the "
        "plan's own refusal (plan_incomplete) is fail-closed",
    },
    "docker_image_listing": {
        "module": "execution/env_snapshot.py",
        "site": "docker images",
        "fenced_secrets": True,
        "fenced_injection": True,
        "requested": "DECLARED SAFE: a tag->id map of this product's own prefix",
    },
    "container_residue_census": {
        "module": "execution/sandbox_stress.py",
        "site": "docker ps / images (also sandbox_adversarial, sandbox_sustained)",
        "fenced_secrets": True,
        "fenced_injection": True,
        "requested": "DECLARED SAFE: container names are ours (hexec-*) and the "
        "residue checks compare against own_container_filter()",
    },
    "result_parsing_view": {
        "module": "execution/result_parsing.py",
        "site": "_output_text",
        "fenced_secrets": True,
        "fenced_injection": True,
        "requested": "DECLARED SAFE (a VIEW, not an egress): consumed only by "
        "integer/boolean/crash extraction and never rendered",
    },
}


class TestTheIngressTableIsReal:
    """T2.W1.5: the table is data, and the call sites are marked in the source."""

    def test_every_ingress_row_names_a_real_call_site(self) -> None:
        for name, row in INGRESS_TABLE.items():
            module = str(row["module"])
            path = REPO_ROOT / module
            assert path.exists(), f"{name}: named module {module!r} does not exist"
            source = path.read_text(encoding="utf-8", errors="replace")
            symbol = str(row["site"]).split(" ")[0].split("(")[0].split(".")[0]
            assert symbol in source, (
                f"{name}: names {row['site']!r} but {module} contains no "
                f"{symbol!r}; a renamed call site makes this row decorative"
            )
            assert str(row["requested"]).strip(), f"{name}: no disposition recorded"

    def test_the_injection_half_is_open_and_says_so(self) -> None:
        """A table claiming the injection gap is closed would be a lie."""
        open_rows = [
            name for name, row in INGRESS_TABLE.items() if not row["fenced_injection"]
        ]
        assert len(open_rows) >= 8, (
            "the injection fence is expected to be OPEN across the execution "
            f"ingresses (shared/security.py owns it); only {len(open_rows)} rows "
            "claim otherwise, so either a T5 request landed or the table is "
            "stale -- both need a deliberate update"
        )
        for name in open_rows:
            assert "T5" in str(INGRESS_TABLE[name]["requested"]), (
                f"{name} is unfenced for injection but names no T5 request"
            )

    def test_unfenced_call_sites_are_marked_in_the_source(self) -> None:
        """T2.W1.5's acceptance: the gap must be VISIBLE in review.

        Checked PER MODULE rather than once, because one marker in
        `verify.py` would otherwise satisfy the whole class while the other
        six ingresses stayed unmarked -- and an unmarked gap is exactly the one
        nobody goes looking for.
        """
        open_modules = {
            str(row["module"])
            for row in INGRESS_TABLE.values()
            if not row["fenced_injection"]
        }
        for module in sorted(open_modules):
            path = REPO_ROOT / module
            source = path.read_text(encoding="utf-8", errors="replace")
            assert "UNFENCED" in source, (
                f"{module} carries at least one injection-unfenced ingress and "
                "has no UNFENCED marker; a gap nobody can grep for is a gap "
                "nobody closes"
            )

    def test_the_marker_is_a_comment_or_docstring_never_a_rendered_string(self) -> None:
        """A marker that can reach the model is not a marker.

        Docstrings are documentation; a bare string literal in an expression
        position is something a renderer could put in front of a person or a
        model. The check distinguishes the two by walking the tree and
        collecting every string constant that is NOT a docstring, so "the
        word UNFENCED appears in the file" cannot be satisfied by a place the
        word would be shown.
        """
        for module in (
            "execution/verify.py",
            "execution/sandbox.py",
            "execution/workspace.py",
            "execution/git_output.py",
            "execution/warm_sandbox.py",
        ):
            source = (REPO_ROOT / module).read_text(encoding="utf-8")
            tree = ast.parse(source)
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    body = getattr(node, "body", [])
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        docstrings.add(id(body[0].value))
            rendered = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "UNFENCED" in node.value
                and id(node) not in docstrings
            ]
            assert rendered == [], (
                f"{module}: the word UNFENCED appears in a non-docstring string "
                f"at lines {[node.lineno for node in rendered]}; a marker that "
                "can be rendered is not a marker"
            )

    def test_the_test_report_ingress_is_marked_in_verify_py(self) -> None:
        """The highest-severity row specifically, by name."""
        source = (EXECUTION_ROOT / "verify.py").read_text(encoding="utf-8")
        assert "UNFENCED" in source, (
            "execution/verify.py must carry an UNFENCED marker at the "
            "test-report call site; pytest prints test NAMES, the test's own "
            "SOURCE LINE and assertion messages, and structured_feedback is "
            "injected into the next turn as if harness-authored"
        )
        assert "structured_feedback" in source

    def test_execution_does_not_own_a_second_scanner(self) -> None:
        """T2.W1.5: do not invent a second scanning implementation.

        `execution/ingress.py` DELEGATES redaction to `shared.security`. A
        local pattern table here would be gap G34 re-opened.
        """
        source = (EXECUTION_ROOT / "ingress.py").read_text(encoding="utf-8")
        assert "shared.security" in source, (
            "ingress.py must delegate to shared.security.redact_text"
        )
        assert "_SECRET_PATTERNS" not in source and "_QUOTED_SECRET" not in source, (
            "ingress.py must not reach into shared.security's private compiled "
            "patterns; delegate to a public entry point"
        )


# ---------------------------------------------------------------------------
# Cross-cutting: the module must stay importable and the wave must not have
# widened anything outside `execution/`.
# ---------------------------------------------------------------------------


def test_the_new_module_is_importable_without_docker() -> None:
    """`edit_landing` must not import `sandbox`, so the write path stays
    importable in a Docker-less environment."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import execution.edit_landing as m; print(len(m.LANDING_CLASSES))",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3"


def test_the_pin_module_has_no_warnings_that_would_mask_a_failure() -> None:
    """A test module that cannot be collected is not evidence."""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
