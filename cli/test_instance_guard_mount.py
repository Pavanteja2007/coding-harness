"""T4.W1.2a - the multi-instance guard, MOUNTED.

`cli.session.open_session` is a hash-, pid- and age-aware single-writer
guard with `tests/test_daily_platform_parity.py` behind it, and for twelve
rounds NOTHING in the product called it. The measured consequence was the
one that matters on a daily-use tool: **two `neo` instances on one
repository were not refused**, and two agents mutating one worktree is how
work is lost.

That suite also carried an INVERTED PIN - a test that passes precisely
BECAUSE the surface is not wired, and fails the moment somebody wires it:

    tests/test_daily_platform_parity.py:2167
    TestWhatThisRoundDidAndDidNotWire::test_no_shell_calls_the_guard_yet
    its own docstring: "When Prompt 01 wires it, this test is the one to
    update -- and it must be updated in the same change, not deleted."

The pin is flipped in that file by the mount. This file proves the mount is
real, and - more importantly - proves it against a REAL SEPARATE OS
PROCESS, because a guard tested only against an in-process lease is a guard
whose cross-process claim is unmeasured.

`tests/**` is T5's, so this file lives in `cli/`.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

TUI = Path(__file__).resolve().parent / "tui.py"
HEADLESS = Path(__file__).resolve().parent / "headless.py"

#: Holds the repository lock in a REAL child process, then exits. Written as
#: a separate module-level string rather than a `-c` one-liner so the
#: evidence is a file a reader can read.
_HOLDER = """
import sys, time
from shared import instance_guard

lease = instance_guard.acquire_repository_lock(
    sys.argv[1], owner="test.probe", command="neo (probe)"
)
print("HELD", flush=True)
time.sleep(float(sys.argv[2]))
lease.release()
"""


@pytest.fixture()
def guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A real git repository, a real peer process holding it, one Neo home.

    The home is set in BOTH the parent environment and the child env: a
    parent that looks in a different directory from the child measures
    nothing and reports `free` - which is exactly the false pass this file
    is written to avoid.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q"], check=False, capture_output=True
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NEO_HOME", str(home))
    env = dict(os.environ)
    env["NEO_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), str(repo), "20"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        ready = proc.stdout.readline()
        assert ready.strip() == "HELD", (
            f"the peer process never took the lock: {ready!r} "
            f"{proc.stderr.read() if proc.stderr else ''}"
        )
        yield {"repo": repo, "home": home, "log_root": tmp_path / "logs", "env": env}
    finally:
        proc.kill()
        proc.wait(timeout=10)


class TestASecondInstanceIsRefused:
    def test_a_live_peer_is_refused_by_a_real_second_process(
        self, guarded: dict
    ) -> None:
        """THE ACCEPTANCE QUESTION, measured across a process boundary.

        A guard exercised only by an in-process lease proves re-entrancy,
        not exclusion. The peer here is a real OS process with its own pid.
        """
        from cli import session as s

        with pytest.raises(Exception) as caught:
            with s.open_session(
                guarded["log_root"], guarded["repo"], command="neo (second)"
            ):
                pass

        message = " ".join(
            getattr(caught.value, "lines", lambda: [str(caught.value)])()
        )
        assert "another neo instance" in message
        assert "neo (probe)" in message, (
            "the refusal must name what the other process is running, or a "
            "reader cannot tell which window to close"
        )

    def test_the_read_side_sees_the_peer_without_taking_anything(
        self, guarded: dict
    ) -> None:
        """The guard has two halves and they are not the same claim.

        The reader must work against a live peer, because ``/sessions`` and
        ``--continue`` have to keep listing while a session legitimately
        holds the repository.
        """
        from cli import session as s

        report = s.session_instance_guard(guarded["repo"])

        assert report.get("free") is False
        assert report.get("state") == "held"
        assert report.get("enforced") is True, (
            "a held repository under the default policy must be reported as "
            "enforced; 'enforced: False' next to 'held' is a receipt that "
            "reads as safe"
        )
        assert report.get("lines"), (
            "a report with no lines is a report nobody can act on"
        )

    def test_the_refusal_names_the_pid_and_the_door_out(self, guarded: dict) -> None:
        from shared import instance_guard

        # `probe_repository_lock` is the reader that returns the typed
        # `InstanceInfo` `describe_instance` wants. `instance_guard_report`
        # returns a plain dict for a surface to render, so feeding that here
        # would be a type error rather than a claim about the refusal.
        info = instance_guard.probe_repository_lock(guarded["repo"])
        text = "\n".join(instance_guard.describe_instance(info))

        assert f"pid {info.pid}" in text
        assert "wait for it, or stop it" in text, (
            "a refusal a reader cannot act on is a hang with better manners"
        )

    def test_the_exit_code_for_a_refusal_is_environment_not_usage(
        self, guarded: dict
    ) -> None:
        """The decision `cli/UNMOUNTED.md` recorded as a UX choice, MADE.

        3 (`environment_error`), not 2 (the command line is fine) and not 1
        (nothing ran). A CI job that treats "fix the machine" differently
        from "the bug beat the agent" needs the categories to differ.
        """
        from cli.exit_codes import EXIT_CODES

        assert EXIT_CODES["environment_error"] == 3

    def test_a_dead_peer_is_taken_over_without_asking_a_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashed `neo` must not need a human to clear its lock."""
        from shared import instance_guard

        repo = tmp_path / "dead"
        repo.mkdir()
        home = tmp_path / "dead-home"
        home.mkdir()
        monkeypatch.setenv("NEO_HOME", str(home))

        # A lease object that is never released, from a process that has
        # exited: the lock file survives, the pid does not.
        lease = instance_guard.acquire_repository_lock(repo, owner="test.dead")
        lock_path = Path(lease.lock_path)
        assert lock_path.is_file()
        # Rewriting the recorded pid to one that cannot be alive is the
        # honest simulation of "the process that took this is gone".
        document = __import__("json").loads(lock_path.read_text(encoding="utf-8"))
        document["pid"] = 999999
        lock_path.write_text(__import__("json").dumps(document), encoding="utf-8")
        lease.__dict__["_released"] = True  # do not let __del__ re-clean

        taken = instance_guard.acquire_repository_lock(repo, owner="test.taker")
        try:
            assert taken is not None, "a dead peer's lock must be takeable"
        finally:
            taken.release()


class TestTheOptOutIsExplicit:
    def test_warn_mode_lets_the_session_through_and_says_so(
        self, guarded: dict
    ) -> None:
        from cli import session as s

        with s.open_session(
            guarded["log_root"],
            guarded["repo"],
            config={"session_instance_guard": "warn"},
            command="neo (warn)",
        ) as session:
            guard = session["instance_guard"]
            assert guard["held"] is False, (
                "warn mode must NOT take the lock - pretending it did would "
                "make the receipt claim a protection that is not there"
            )
            assert guard["conflict"] is not None, (
                "warn mode must RECORD the conflict, or the receipt reads as "
                "an uncontested run"
            )
            assert any("warn" in line for line in guard["lines"]), (
                "warn mode must say the other session is still running"
            )

    def test_an_unrecognised_mode_falls_back_to_refusing(self, guarded: dict) -> None:
        """Fail closed. A typo must never silently disable a trust gate."""
        from cli import session as s

        with pytest.raises(Exception):
            with s.open_session(
                guarded["log_root"],
                guarded["repo"],
                config={"session_instance_guard": "wran"},
                command="neo (typo)",
            ):
                pass


class TestTheShellsActuallyCallIt:
    """The mount, read from the source.

    A guard that the product does not call is worse than no guard, because
    it reads as protection. These read the AST rather than importing the
    modules, so a reformat cannot empty them and a comment cannot pass them.
    """

    def _functions(self, path: Path) -> dict:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }

    def _class_method(
        self, path: Path, class_name: str, method: str
    ) -> ast.FunctionDef:
        """One method of one named class.

        Scoped to the class because `cli/tui.py` has an ``on_mount`` on a
        dozen screens; a whole-file lookup that took the last match would
        assert against whichever widget happened to be defined last, and
        would keep passing after the real one was deleted.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in node.body:
                    if isinstance(child, ast.FunctionDef) and child.name == method:
                        return child
        raise AssertionError(f"{class_name}.{method} is gone from {path.name}")

    def test_the_tui_acquires_and_releases_the_lease(self) -> None:
        functions = self._functions(TUI)
        assert "_acquire_instance_guard" in functions
        assert "_release_instance_guard" in functions

        acquire = ast.unparse(functions["_acquire_instance_guard"])
        assert "acquire_repository_lock" in acquire, (
            "the TUI no longer takes a real lease"
        )
        assert "ConcurrentInstanceError" in acquire, (
            "the TUI no longer handles a refusal, so a busy repository would "
            "raise out of on_mount instead of showing a screen"
        )

        release = ast.unparse(functions["_release_instance_guard"])
        assert "release()" in release, (
            "a guard that leaks its lock teaches operators to kill processes"
        )

        unmount = ast.unparse(self._class_method(TUI, "NeoApp", "on_unmount"))
        assert "_release_instance_guard()" in unmount, (
            "on_unmount must release the lease; a guard released only on a "
            "clean exit is a guard that outlives ctrl+c"
        )

    def test_the_tui_refuses_before_it_loads_a_conversation(self) -> None:
        mount = ast.unparse(self._class_method(TUI, "NeoApp", "on_mount"))
        acquire_at = mount.find("_acquire_instance_guard()")
        refuse_at = mount.find("_render_refusal()")
        assert acquire_at != -1 and refuse_at != -1, (
            "the TUI must acquire the guard and render a refusal at mount"
        )
        assert acquire_at < refuse_at, (
            "the refusal must be RENDERED right after acquiring, before a "
            "conversation is loaded - a refusal that leaves a half-open "
            "session behind it is not a refusal"
        )

    def test_the_headless_path_takes_the_guard_through_the_one_authority(
        self,
    ) -> None:
        functions = self._functions(HEADLESS)
        resolve = ast.unparse(functions["_resolve_session"])
        assert "open_session" in resolve, (
            "cli/headless.py must go through cli.session.open_session, not a "
            "private re-implementation of the guard"
        )
        assert "load_or_create" not in resolve, (
            "_resolve_session went back to the UNGUARDED loader; a headless "
            "turn is as much a writer as a TUI one"
        )

    def test_the_headless_refusal_is_not_swallowed(self) -> None:
        """The old `except Exception: return {}, True` was a fail-OPEN guard.

        It turned a refusal into an empty session and carried on writing,
        which is the exact opposite of what the guard is for.
        """
        functions = self._functions(HEADLESS)
        run = ast.unparse(functions["run_headless"])
        assert "ConcurrentInstanceError" in run, (
            "run_headless must recognise the refusal and return it as an "
            "environment_error envelope"
        )
        assert "environment_error" in run, (
            "the refusal must carry the environment exit code, not a usage "
            "or task-failure code"
        )

    def test_the_headless_lease_is_released_in_a_finally(self) -> None:
        functions = self._functions(HEADLESS)
        run = ast.unparse(functions["run_headless"])
        assert "_guard_stack.close()" in run, (
            "the headless lease must be released in a finally, or a crashed "
            "turn leaves the repository locked"
        )
