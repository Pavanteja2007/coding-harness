"""Tests for execution.sandbox (Docker-gated where the daemon is needed).

Layout:
- unit tests (no Docker): path conversion, fingerprinting, Dockerfile
  generation, argv assembly, truncation — these must run everywhere;
- integration tests (Docker required): marked `docker`, skipped with an
  explicit reason when the daemon is down. Set HARNESS_EXEC_SKIP_DOCKER=1
  to force-skip them (e.g. in CI without docker).

The integration tests bind-mount a tmp_path repo. Docker Desktop on
Windows shares C:\\Users by default — tmp_path lives under the user's
AppData\\Local\\Temp which is on C:. On exotic setups (tmp on an unshared
drive) the mount fails; that surfaces as an error with docker's own
message, which is the correct behavior (fail loud, not silent host run).
"""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from execution import sandbox as sb
from shared.types import ExecutionResult


def _docker_up() -> bool:
    """Daemon reachable? (cache-free so the marker reflects reality)"""
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)


# ---------------------------------------------------------------------------
# Unit tests — no Docker needed
# ---------------------------------------------------------------------------

class TestPathConversion:
    def test_windows_drive_path_uses_forward_slashes(self):
        p = sb._win_to_docker(r"C:\Users\pavan\repo")
        assert p == "C:/Users/pavan/repo"

    def test_posix_path_unchanged(self, monkeypatch):
        monkeypatch.setattr(sb.os, "name", "posix")
        assert sb._win_to_docker("/home/u/repo") == "/home/u/repo"

    def test_windows_posix_style_passthrough(self):
        # Already-forward-slash Windows path passes through unchanged.
        assert sb._win_to_docker("C:/x/y") == "C:/x/y"


class TestFingerprint:
    def test_stable_and_order_sensitive(self):
        assert sb._fingerprint(["a", "b"]) == sb._fingerprint(["a", "b"])
        assert sb._fingerprint(["a", "b"]) != sb._fingerprint(["b", "a"])

    def test_short(self):
        assert len(sb._fingerprint(["x"])) == 12


class TestDockerfileGeneration:
    def test_repo_dockerfile_is_static_shape(self):
        text = sb._repo_dockerfile_text("harness-exec:abc123")
        assert text.startswith("FROM harness-exec:base")
        assert "harness.dep-image=\"harness-exec:abc123\"" in text
        assert "_extract_pyproject_deps.py" in text
        # pip install must be conditional, never `|| true` (masked failures)
        assert "|| true" not in text

    def test_extractor_script_is_valid_python(self, tmp_path):
        # The generated script must compile on the host — quote-escaping
        # bugs in the template show up here, not in a docker build failure.
        script = tmp_path / "_extract.py"
        script.write_text(sb._EXTRACT_PYPROJECT_DEPS, encoding="utf-8")
        compile(script.read_text(encoding="utf-8"), str(script), "exec")

    def test_base_dockerfile_bakes_pytest_and_tomli(self):
        text = sb._base_dockerfile_text()
        assert "pytest" in text and "tomli" in text


class TestRunArgs:
    def test_network_off_by_default(self):
        args = sb._docker_run_args("img", r"C:\repo", 30, False, None,
                                   "1g", 1.0, 512)
        assert ["--network", "none"] == [args[i:i + 2] for i in
                                         range(len(args) - 1) if args[i] == "--network"][0]
        assert "--rm" in args
        assert "--read-only" in args
        assert "--cap-drop" in args and "ALL" in args
        assert "--pull=never" in args
        assert any("/workspace" in a for a in args)

    def test_network_optin_removes_none(self):
        args = sb._docker_run_args("img", "/r", 30, True, None, "1g", 1.0, 512)
        assert "--network" not in args

    def test_env_vars_passed_through(self):
        args = sb._docker_run_args("img", "/r", 30, False, {"FOO": "bar"},
                                  "1g", 1.0, 512)
        assert "FOO=bar" in args

    def test_windows_mount_path_forward_slashes(self):
        args = sb._docker_run_args("img", r"C:\repo", 30, False, None,
                                   "1g", 1.0, 512)
        mount = args[args.index("--volume") + 1]
        assert mount.startswith("C:/repo") and mount.endswith(":/workspace")

    def test_container_name_extractable(self):
        args = ["docker", "run", "--name", "hexec-abc", "img"]
        assert sb._container_name(args) == "hexec-abc"

    def test_container_name_missing(self):
        assert sb._container_name(["docker", "run", "img"]) == ""

    def test_container_name_embeds_owner_pid(self):
        # PID-embedded names are the orphan-reap contract: the name must
        # carry the owning process's PID as a parseable token.
        args = sb._docker_run_args("img", "/r", 30, False, None, "1g", 1.0, 512)
        name = args[args.index("--name") + 1]
        assert name.startswith(f"{sb.CONTAINER_PREFIX}-p")
        pid = sb._container_pid_from_name(name)
        assert pid == os.getpid()


class TestOrphanReapNames:
    def test_pid_parsed_from_new_format(self):
        assert sb._container_pid_from_name("hexec-p1234-a1b2c3") == 1234

    def test_pid_none_from_old_format(self):
        # Old pre-PID names are UNKNOWABLE owners — must never be reaped.
        assert sb._container_pid_from_name("hexec-a1b2c3d4e5f6") is None

    def test_pid_none_for_garbage(self):
        assert sb._container_pid_from_name("nginx-proxy") is None
        assert sb._container_pid_from_name("") is None
        assert sb._container_pid_from_name("hexec-p-abc") is None

    def test_pid_alive_self_and_dead(self):
        assert sb._pid_alive(os.getpid()) is True
        assert sb._pid_alive(0) is False
        assert sb._pid_alive(-5) is False
        # A recycled-but-unlikely pid: no assertion on liveness, just
        # that it doesn't raise.
        sb._pid_alive(999_999_999)


class TestCrossProcLock:
    def test_acquire_release(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.tempfile, "gettempdir", lambda: str(tmp_path))
        lock = sb._CrossProcLock("t-acq")
        with lock as acquired:
            assert acquired is True
        # released: a second acquire works
        with sb._CrossProcLock("t-acq") as acquired:
            assert acquired is True

    def test_second_holder_reports_peer_alive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.tempfile, "gettempdir", lambda: str(tmp_path))
        lock = sb._CrossProcLock("t-peer")
        with lock:
            other = sb._CrossProcLock("t-peer")
            assert other.holder_alive() is False  # not acquired yet
            # busy-wait times out (5s default); shave it for test speed
            import time as _t
            _start = _t.monotonic()
            with monkeypatch.context() as m:
                m.setattr(sb._CrossProcLock, "__enter__", lambda self: False)
                assert other.__enter__() is False
            other._peer_pid = os.getpid()
            assert other.holder_alive() is True

    def test_stale_lock_stolen_when_holder_dead(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.tempfile, "gettempdir", lambda: str(tmp_path))
        lock_path = tmp_path / "t-stale.lock"
        lock_path.write_text("999999999")  # a pid that cannot exist
        lock = sb._CrossProcLock("t-stale")
        with lock as acquired:
            assert acquired is True  # stolen from the dead holder
        assert not lock_path.exists()  # released by the thief

    def test_maybe_reap_rate_limited(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sb, "reap_orphaned_containers",
                            lambda **kw: calls.append(kw) or [])
        import execution.sandbox as live_sb
        live_sb._last_reap_ts = 0.0
        sb._maybe_reap()
        sb._maybe_reap()  # within the interval: must NOT sweep again
        assert len(calls) == 1


class TestTruncate:
    def test_short_text_unchanged(self):
        assert sb._truncate("hello", 100) == "hello"

    def test_long_text_keeps_head_tail_and_marker(self):
        text = "x" * 10_000
        out = sb._truncate(text, 100)
        assert "chars omitted" in out
        assert out.startswith("x") and out.endswith("x")
        assert len(out) < 200  # bounded by limit + marker


class TestSandboxUnavailable:
    def test_raises_when_docker_down(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sb, "docker_available", lambda: False)
        with pytest.raises(sb.SandboxUnavailableError):
            sb.execute_sandboxed(str(tmp_path), "echo hi", 30)

    def test_raises_for_missing_repo(self, monkeypatch):
        monkeypatch.setattr(sb, "docker_available", lambda: True)
        with pytest.raises(FileNotFoundError):
            sb.execute_sandboxed(r"C:\definitely\not\here", "echo hi", 30)


# ---------------------------------------------------------------------------
# Integration tests — Docker required (marker: docker)
# ---------------------------------------------------------------------------

def _mkrepo(tmp_path: Path) -> Path:
    """Tiny repo the integration tests can mount (marker + one module)."""
    (tmp_path / "mymod.py").write_text("def add(a, b):\n    return a + b\n",
                                       encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\".\"]\n", encoding="utf-8")
    return tmp_path


@requires_docker
class TestSandboxIntegration:
    def test_command_runs_and_captures(self, tmp_path):
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(str(repo), "echo hello-sandbox", 120)
        assert res.exit_code == 0
        assert "hello-sandbox" in res.stdout
        assert not res.timed_out

    def test_exit_code_and_stderr_flow_back(self, tmp_path):
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo), "python -c \"import sys; sys.stderr.write('boom'); sys.exit(3)\"", 120)
        assert res.exit_code == 3
        assert "boom" in res.stderr

    def test_edits_persist_to_host(self, tmp_path):
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(str(repo), "echo persisted > made.txt", 120)
        assert res.exit_code == 0
        assert (repo / "made.txt").read_text(encoding="utf-8").strip() == "persisted"

    def test_network_blocked_by_default(self, tmp_path):
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo),
            "python -c \"import socket\n"
            "try:\n"
            "    socket.create_connection(('example.com', 80), 3)\n"
            "    print('NET-OPEN')\n"
            "except Exception as e:\n"
            "    print('NET-BLOCKED', type(e).__name__)\"",
            120,
        )
        assert res.exit_code == 0
        assert "NET-BLOCKED" in res.stdout
        assert "NET-OPEN" not in res.stdout

    def test_timeout_kills_container(self, tmp_path):
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(str(repo), "sleep 60", 5)
        assert res.timed_out is True
        assert res.exit_code == 124

    def test_memory_limit_enforced(self, tmp_path):
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo), "python -c \"a=[0]*10**9\"", 120)
        assert res.exit_code != 0  # 137 (OOM) on linux — nonzero suffices
        assert not res.timed_out

    def test_no_container_left_behind(self, tmp_path):
        repo = _mkrepo(tmp_path)
        sb.execute_sandboxed(str(repo), "true", 120)
        ls = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=hexec-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
        assert ls.stdout.strip() == ""

    def test_dep_image_reused_across_calls(self, tmp_path):
        repo = _mkrepo(tmp_path)
        tag = sb.ensure_image(str(repo))
        assert sb.ensure_image(str(repo)) == tag  # cached, no rebuild

    def test_orphaned_container_reaped_by_peer(self, tmp_path):
        """The concurrency-hardening core guarantee: a container whose
        owning process is hard-killed (scheduler crash kill) gets reaped
        by a SURVIVING process, not left burning VM resources until its
        command finishes."""
        import subprocess
        import sys
        import textwrap

        (tmp_path / "victim_repo").mkdir()
        repo = _mkrepo(tmp_path / "victim_repo")
        # child: start a long-running container, then get hard-killed
        # from outside (no cleanup chance, like a scheduler crash kill).
        child = tmp_path / "orphan_child.py"
        child.write_text(textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path(sb.__file__).parents[1])!r})
            from execution.sandbox import execute_sandboxed
            execute_sandboxed({str(repo)!r}, "sleep 120", 110)
        """), encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(child)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        # wait for the child's container to actually be running
        deadline = time.time() + 60
        running = []
        while time.time() < deadline:
            ls = subprocess.run(
                ["docker", "ps", "--filter", "name=hexec-", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=30)
            running = [n for n in ls.stdout.split()
                       if n.startswith(f"hexec-p{proc.pid}-")]
            if running:
                break
            time.sleep(1.0)
        assert running, "child never started its container"

        # hard-kill the child (TerminateProcess semantics, no cleanup)
        proc.kill()
        proc.wait(timeout=30)
        time.sleep(2.0)  # let the CLI pipe close; container stays Up

        ls = subprocess.run(
            ["docker", "ps", "--filter", f"name={running[0]}", "--format",
             "{{.Names}} {{.Status}}"], capture_output=True, text=True, timeout=30)
        assert running[0] in ls.stdout, "container should be orphaned-but-alive"

        # reap from THIS process (the surviving peer):
        killed = sb.reap_orphaned_containers()
        assert running[0] in killed
        time.sleep(3.0)  # --rm removal is daemon-async after kill
        ls2 = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={running[0]}", "--format",
             "{{.Names}}"], capture_output=True, text=True, timeout=30)
        assert ls2.stdout.strip() == "", "orphan not removed after reap"

    def test_concurrent_burst_no_leak(self, tmp_path):
        """20 simultaneous execute_sandboxed calls from thread pool:
        all succeed, all clean up (no hexec- residue), image cache
        unchanged (same repo -> same dep image)."""
        from concurrent.futures import ThreadPoolExecutor

        repo = _mkrepo(tmp_path)
        tag_before = sb._dep_image_tag(str(repo))
        n_images_before = subprocess.run(
            ["docker", "images", "--filter", "reference=harness-exec*",
             "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, timeout=30).stdout.split()

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(sb.execute_sandboxed, str(repo),
                                   f"echo burst-{i}", 120)
                       for i in range(20)]
            results = [f.result(timeout=180) for f in futures]
        assert all(r.exit_code == 0 for r in results)
        assert all(f"burst-{i}" in results[i].stdout for i in range(20))

        ls = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=hexec-",
             "--format", "{{.Names}}"], capture_output=True, text=True,
            timeout=30)
        assert ls.stdout.strip() == ""
        n_images_after = subprocess.run(
            ["docker", "images", "--filter", "reference=harness-exec*",
             "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, timeout=30).stdout.split()
        assert n_images_before == n_images_after  # no cache growth
        assert tag_before in n_images_after
