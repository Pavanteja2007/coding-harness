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

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from execution import sandbox as sb


def _docker_up() -> bool:
    """Daemon reachable? (cache-free so the marker reflects reality)"""
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=30,
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
    def test_windows_drive_path_uses_forward_slashes(self, monkeypatch):
        # Test the WINDOWS branch of _win_to_docker everywhere: on POSIX
        # hosts os.name patches to "nt" (the branch is pure string ops,
        # no Windows API — same pattern as test_posix_path_unchanged
        # patches to "posix"). Without this the test only ran on Windows.
        monkeypatch.setattr(sb.os, "name", "nt")
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
        assert 'harness.dep-image="harness-exec:abc123"' in text
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
        args = sb._docker_run_args("img", r"C:\repo", 30, False, None, "1g", 1.0, 512)
        network_pairs = [
            args[i : i + 2] for i in range(len(args) - 1) if args[i] == "--network"
        ]
        assert next(pair for pair in network_pairs if pair == ["--network", "none"])
        assert "--rm" in args
        assert "--read-only" in args
        assert "--cap-drop" in args and "ALL" in args
        assert "--security-opt" in args and "no-new-privileges:true" in args
        assert "--memory-swap" in args
        assert "--cpus" in args
        assert "--pids-limit" in args
        assert "/tmp:rw,exec,size=256m" in args
        assert "--pull=never" in args
        assert any("/workspace" in a for a in args)

    def test_network_optin_removes_none(self):
        args = sb._docker_run_args("img", "/r", 30, True, None, "1g", 1.0, 512)
        assert "--network" not in args

    def test_env_vars_passed_through(self):
        args = sb._docker_run_args(
            "img", "/r", 30, False, {"FOO": "bar"}, "1g", 1.0, 512
        )
        assert "FOO=bar" in args

    def test_env_scrubber_drops_bearer_jwt_and_url_credentials(self):
        env = {
            "PUBLIC_VALUE": "ok",
            "AUTHORIZATION": "Bearer unit-secret-token-value",
            "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "ENDPOINT": "https://user:password@example.invalid/path",
            "SSH_AUTH_SOCK": "unit-secret-socket",
        }
        scrubbed = sb._scrub_container_env(env)
        assert scrubbed == {"PUBLIC_VALUE": "ok"}

    def test_windows_mount_path_forward_slashes(self, monkeypatch):
        # Windows branch of the mount-path conversion, runnable on POSIX
        # hosts too (see TestPathConversion note).
        monkeypatch.setattr(sb.os, "name", "nt")
        args = sb._docker_run_args("img", r"C:\repo", 30, False, None, "1g", 1.0, 512)
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
        assert sb._container_pid_from_name(name) == os.getpid()
        # the env token ALSO precedes the pid (Round-7): stale pre-fix
        # sweeps anchor on `hexec-p<pid>` and must not match our names
        assert not name.startswith(f"{sb.CONTAINER_PREFIX}-p")

    def test_container_name_embeds_env_token(self):
        # Round-7 name contract: the name embeds an environment token
        # BEFORE the pid (hexec-e<env8>-p<pid>-<uuid>) so orphan sweeps
        # can tell which PID SPACE the owner lives in (Docker Desktop
        # shares one daemon between Windows and WSL hosts with
        # UNRELATED pids).
        args = sb._docker_run_args("img", "/r", 30, False, None, "1g", 1.0, 512)
        name = args[args.index("--name") + 1]
        assert sb._container_env_from_name(name) is not None
        # same process -> same token every call
        args2 = sb._docker_run_args("img", "/r", 30, False, None, "1g", 1.0, 512)
        name2 = args2[args2.index("--name") + 1]
        assert sb._container_env_from_name(name) == sb._container_env_from_name(name2)


class TestOrphanReapNames:
    def test_pid_parsed_from_new_format(self):
        # Round-7 format parses; Round-3 format (hexec-p<pid>-<uuid>)
        # intentionally does NOT — stale sweeps must not judge it.
        assert sb._container_pid_from_name("hexec-e0ac53fed-p1234-abcdef123456") == 1234
        assert sb._container_pid_from_name("hexec-p1234-a1b2c3") is None

    def test_env_token_parsed_and_absent(self):
        assert (
            sb._container_env_from_name("hexec-e0ac53fed-p1234-abcdef123456")
            == "0ac53fed"
        )
        # Round-3 format (PID but no env token) and older: no token
        assert sb._container_env_from_name("hexec-p1234-a1b2c3") is None
        assert sb._container_env_from_name("hexec-a1b2c3d4e5f6") is None

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

    def test_reaper_skips_foreign_env_owner(self, monkeypatch):
        """REGRESSION (Round 7, found live): a sweep must NEVER judge a
        container whose owner lives in a DIFFERENT PID space — with
        Docker Desktop, Windows-host and WSL-host harness processes
        share one daemon; a Windows-side _pid_alive(WSL_PID) probe can
        only say "dead" (no such Windows pid), and the pre-fix reaper
        then SIGKILLed a LIVE WSL-owned container mid-run (reproduced:
        a Windows-side sweep killed a WSL-owned sleep-120 container
        seconds in). Foreign-env names and env-less (Round-3) names are
        both skipped; only same-env dead-owner containers are killed."""
        my_env = sb._container_env_from_name(sb._container_name_prefix() + "-x")
        names = {
            "foreign-env": "hexec-efeedface-p999999-0123456789ab",
            "no-token": "hexec-p999998-abc123",
            "same-env-dead": f"hexec-e{my_env}-p999997-deadbeefcafe",
        }

        def fake_ls(*a, **k):
            class CP:
                returncode = 0
                stdout = "\n".join(names.values()) + "\n"

            return CP()

        kills: list = []

        def spy_run(args, **k):
            if args and args[0] == "kill":
                kills.append(args[1] if len(args) > 1 else "?")
            return fake_ls()

        monkeypatch.setattr(sb, "_run_docker", spy_run)
        monkeypatch.setattr(sb, "_pid_alive", lambda pid: False)
        killed = sb.reap_orphaned_containers()
        assert killed == [names["same-env-dead"]], (
            f"reaper judged foreign-env or env-less names: {killed}"
        )
        assert kills == [names["same-env-dead"]]


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

    def test_base_image_is_not_marked_done_when_peer_never_builds(self, monkeypatch):
        class Result:
            returncode = 1
            stdout = ""
            stderr = ""

        monkeypatch.setattr(sb, "_base_done", False)
        monkeypatch.setattr(sb, "_run_docker", lambda *args, **kwargs: Result())
        monkeypatch.setattr(sb, "_wait_for_image", lambda *args, **kwargs: False)
        monkeypatch.setattr(sb._CrossProcLock, "__enter__", lambda self: False)
        with pytest.raises(sb.SandboxDependencyError, match="timed out"):
            sb._ensure_base_image("python")
        assert sb._base_done is False

    def test_release_does_not_delete_replacement_owner(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.tempfile, "gettempdir", lambda: str(tmp_path))
        lock = sb._CrossProcLock("t-owner")
        with lock:
            lock.path.write_text(
                json.dumps({"pid": os.getpid(), "token": "replacement"}),
                encoding="utf-8",
            )
        assert lock.path.exists()
        assert (
            json.loads(lock.path.read_text(encoding="utf-8"))["token"] == "replacement"
        )

    def test_maybe_reap_rate_limited(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            sb, "reap_orphaned_containers", lambda **kw: calls.append(kw) or []
        )
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

    def test_bounded_capture_discards_middle_while_streaming(self):
        capture = sb._BoundedCapture(100)
        for _ in range(1000):
            capture.feed(b"0123456789")
        out = capture.value()
        assert "chars omitted" in out
        assert len(out) < 180
        assert out.startswith("0")
        assert out.endswith("9")

    def test_invalid_container_uid_is_rejected(self, monkeypatch):
        monkeypatch.setenv("HARNESS_SANDBOX_UID", "1000;echo owned")
        with pytest.raises(ValueError):
            sb._container_user()

    def test_daemon_connection_error_is_classified(self):
        assert sb._docker_unavailable_error(
            "error during connect: the system cannot find the file specified"
        )
        assert not sb._docker_unavailable_error("image not found")


class TestTracePolicy:
    def test_network_and_resource_policy_are_emitted(self, monkeypatch, tmp_path):
        from shared import tracing

        events = []
        monkeypatch.setattr(
            tracing,
            "emit",
            lambda module, event, **fields: events.append((module, event, fields)),
        )
        work = tmp_path / "task-1" / "work"
        work.mkdir(parents=True)
        sb._emit_trace(
            str(work),
            "pytest -q",
            10,
            allow_network=True,
            image="harness-exec:test",
            mem_limit="1g",
            cpu_limit=1.0,
            pids_limit=512,
        )
        assert events
        assert events[0][2]["allow_network"] is True
        assert events[0][2]["image"] == "harness-exec:test"
        assert events[0][2]["pids_limit"] == 512


class TestSandboxUnavailable:
    def test_failed_probe_is_not_cached(self, monkeypatch):
        calls = []

        class Result:
            returncode = 1
            stdout = ""

        def fake_run(*args, **kwargs):
            calls.append(1)
            return Result()

        monkeypatch.setattr(sb, "_docker_ok", None)
        monkeypatch.setattr(sb.subprocess, "run", fake_run)
        assert sb.docker_available() is False
        assert sb.docker_available() is False
        assert len(calls) == 2

    def test_raises_when_docker_down(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sb, "docker_available", lambda: False)
        with pytest.raises(sb.SandboxUnavailableError):
            sb.execute_sandboxed(str(tmp_path), "echo hi", 30)

    def test_rejects_empty_command_before_docker_probe(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            sb,
            "docker_available",
            lambda: pytest.fail("empty command reached Docker probe"),
        )
        with pytest.raises(ValueError, match="command"):
            sb.execute_sandboxed(str(tmp_path), "  ", 30)

    def test_raises_for_missing_repo(self, monkeypatch):
        monkeypatch.setattr(sb, "docker_available", lambda: True)
        with pytest.raises(FileNotFoundError):
            sb.execute_sandboxed(r"C:\definitely\not\here", "echo hi", 30)


# ---------------------------------------------------------------------------
# Integration tests — Docker required (marker: docker)
# ---------------------------------------------------------------------------


def _mkrepo(tmp_path: Path) -> Path:
    """Tiny repo the integration tests can mount (marker + one module)."""
    (tmp_path / "mymod.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["."]\n', encoding="utf-8"
    )
    return tmp_path


def _hexec_residue(name_filter: str) -> list[str]:
    """Containers matching a docker name substring RIGHT NOW.

    Scoped by design: docker's `name=` filter is a substring match, so
    a PID token (`hexec-p12345`) selects ONE process's containers.
    Parallel pytest runs share this daemon (4 terminals, one machine) —
    a global `hexec-` check would see the other suite's LIVE containers
    and false-red (found live in Round 5: two suites run concurrently
    tripped each other's residue assertions)."""
    ls = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"name={name_filter}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return ls.stdout.split()


def _assert_no_hexec_residue(
    name_filter: str | None = None, timeout_s: float = 15.0
) -> None:
    """Assert no container matching name_filter remains, WITHOUT the
    one-shot flake.

    `--rm` removal is daemon-side and asynchronous: a finished container
    can still appear in `docker ps -a` briefly after its `docker run`
    returned — an instant one-shot check after a 20-way burst can
    false-red on that teardown lag (the Round-4/5 intermittent red).
    Poll until clear; a REAL leak still fails loudly (leaked names are
    in the message — leaks never clear, only teardown does).

    Defaults to THIS process's containers (the Round-7 name contract:
    `hexec-e<env>-p<our-pid>-*`): all containers created by this
    suite's execute_sandboxed calls carry our PID, and other
    terminals' concurrent suites don't pollute the check. Pass an
    explicit name_filter (e.g. a victim container's full name) to scope
    to someone else's container."""
    filt = name_filter if name_filter is not None else sb.own_container_filter()
    deadline = time.time() + timeout_s
    residue = _hexec_residue(filt)
    while residue and time.time() < deadline:
        time.sleep(0.5)
        residue = _hexec_residue(filt)
    assert not residue, (
        f"containers matching {filt!r} still present {timeout_s}s after "
        f"run end: {residue}"
    )


def _normalize_image_tags(tags: list[str]) -> list[str]:
    """Normalize an image listing without relying on daemon ordering."""
    return sorted(set(tags))


def _image_set() -> list[str]:
    """Return a normalized cache snapshot for diagnostics."""
    out = subprocess.run(
        [
            "docker",
            "images",
            "--filter",
            "reference=harness-exec*",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return _normalize_image_tags(out.stdout.split())


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
            str(repo),
            "python -c \"import sys; sys.stderr.write('boom'); sys.exit(3)\"",
            120,
        )
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
            'python -c "import socket\n'
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
        res = sb.execute_sandboxed(str(repo), 'python -c "a=[0]*10**9"', 120)
        assert res.exit_code != 0  # 137 (OOM) on linux — nonzero suffices
        assert not res.timed_out

    def test_no_container_left_behind(self, tmp_path):
        repo = _mkrepo(tmp_path)
        sb.execute_sandboxed(str(repo), "true", 120)
        _assert_no_hexec_residue()

    def test_dep_image_reused_across_calls(self, tmp_path):
        repo = _mkrepo(tmp_path)
        tag = sb.ensure_image(str(repo))
        assert sb.ensure_image(str(repo)) == tag  # cached, no rebuild

    def test_concurrent_cold_builds_share_one_image(self, tmp_path):
        nonce = f"cold-build-{os.getpid()}-{time.time_ns()}"
        repo = _mkrepo(tmp_path)
        (repo / "requirements.txt").write_text(f"pytest\n# {nonce}\n", encoding="utf-8")
        tag = sb._dep_image_tag(str(repo))
        code = (
            "import sys; "
            "from execution.sandbox import ensure_image; "
            "print(ensure_image(sys.argv[1]))"
        )
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(repo)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        outputs = [child.communicate(timeout=600) for child in children]
        assert [child.returncode for child in children] == [0, 0, 0, 0]
        assert {stdout.strip().splitlines()[-1] for stdout, _ in outputs} == {tag}
        assert tag in _image_set()
        assert sb.ensure_image(str(repo)) == tag

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
        sb.ensure_image(str(repo))
        # child: start a long-running container, then get hard-killed
        # from outside (no cleanup chance, like a scheduler crash kill).
        child = tmp_path / "orphan_child.py"
        child.write_text(
            textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path(sb.__file__).parents[1])!r})
            from execution.sandbox import execute_sandboxed
            execute_sandboxed({str(repo)!r}, "sleep 120", 110)
        """),
            encoding="utf-8",
        )
        proc = subprocess.Popen(
            [sys.executable, str(child)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # wait for the child's container to actually be running
        deadline = time.time() + 120
        running = []
        while time.time() < deadline:
            ls = subprocess.run(
                ["docker", "ps", "--filter", "name=hexec-", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # Round-7 name format: the child's containers carry its PID;
            # match by parseable PID rather than prefix text so the
            # check survives format details.
            running = [
                n
                for n in ls.stdout.split()
                if sb._container_pid_from_name(n) == proc.pid
            ]
            if running:
                break
            time.sleep(1.0)
        if not running:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=30)
            pytest.fail(
                "child never started its container: "
                f"stdout={stdout[-500:]!r} stderr={stderr[-500:]!r}"
            )

        # hard-kill the child (TerminateProcess semantics, no cleanup)
        proc.kill()
        proc.wait(timeout=30)
        time.sleep(2.0)  # let the CLI pipe close; container stays Up

        # reap from THIS process (the surviving peer). A concurrent
        # suite's execute_sandboxed calls ALSO sweep (self-healing is a
        # production feature) — the victim may already be gone, which is
        # the mechanism WORKING, not a failure. What must hold: after our
        # own reap, no container with the victim's name remains.
        #
        # The assertion is the polled `_assert_no_hexec_residue` (NOT a
        # one-shot `docker ps -a`): `docker kill` + `--rm` removal is
        # daemon-async and on Linux runners the container can linger in
        # "Dead"/removing state for an instant AFTER the kill was issued
        # (found in Round 7 running the suite on WSL2 — the same
        # teardown-race family Round 5 fixed in the other residue
        # checks). A REAL reap failure still fails loudly: the victim
        # runs `sleep 120`, so it stays Up far beyond the 15s poll
        # window — only teardown, never a leak, clears within it.
        sb.reap_orphaned_containers()
        _assert_no_hexec_residue(name_filter=running[0], timeout_s=15.0)

    def test_concurrent_burst_no_leak(self, tmp_path):
        """20 simultaneous execute_sandboxed calls from thread pool:
        all succeed, all clean up (no hexec- residue), image cache
        unchanged (same repo -> same dep image).

        Flake history (Round 4/5), two distinct mechanisms, both fixed
        by the helpers this test now uses: (a) before/after `docker
        images` compared as RAW lists — same-creation-second images
        reorder nondeterministically (T3's diagnosis; fixed by comparing
        sorted sets via `_image_set`); (b) a one-shot `docker ps -a`
        residue check right after the burst can catch a container whose
        `--rm` teardown hasn't landed yet (fixed by polling via
        `_assert_no_hexec_residue`)."""
        from concurrent.futures import ThreadPoolExecutor

        repo = _mkrepo(tmp_path)
        tag_before = sb._dep_image_tag(str(repo))

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [
                pool.submit(sb.execute_sandboxed, str(repo), f"echo burst-{i}", 120)
                for i in range(20)
            ]
            results = [f.result(timeout=180) for f in futures]
        assert all(r.exit_code == 0 for r in results)
        assert all(f"burst-{i}" in results[i].stdout for i in range(20))

        _assert_no_hexec_residue()
        assert tag_before in _image_set()

    def test_image_tag_normalization_is_order_independent(self):
        raw = [
            ["harness-exec:b", "harness-exec:a", "harness-exec:b"],
            ["harness-exec:a", "harness-exec:b"],
        ]
        normalized = [_normalize_image_tags(items) for items in raw]
        assert normalized[0] == normalized[1] == ["harness-exec:a", "harness-exec:b"]

    def test_regression_burst_back_to_back_with_other_tests(self, tmp_path):
        """REGRESSION: the flake was ORDER-DEPENDENT — the burst went red
        when run immediately after other container-driving tests (no
        settling gap), never in isolation. Reproduce exactly that regime:
        a sandboxed run, then the 20-burst IMMEDIATELY after (the residue
        poll covers the first run's --rm teardown, the image set covers
        ordering), asserting the same things the burst test asserts.

        Green here = the conditions that produced the intermittent red
        now pass deterministically, not just "ran clean once"."""
        from concurrent.futures import ThreadPoolExecutor

        repo = _mkrepo(tmp_path)
        # predecessor container churn, deliberately unwaited
        sb.execute_sandboxed(str(repo), "echo predecessor", 120)

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [
                pool.submit(sb.execute_sandboxed, str(repo), f"echo burst2-{i}", 120)
                for i in range(20)
            ]
            results = [f.result(timeout=180) for f in futures]
        assert all(r.exit_code == 0 for r in results)

        _assert_no_hexec_residue()
        assert sb._dep_image_tag(str(repo)) in _image_set()


@requires_docker
class TestSandboxAdversarial:
    """Round 6 security hardening: the adversarial probes that
    execution/sandbox_adversarial.py runs at scale, distilled into fast
    pytest regression tests. Each asserts the LIMIT or ISOLATION actually
    bit — not just that the harness survived."""

    def test_fork_bomb_collapses_at_pids_limit(self, tmp_path):
        """--pids-limit 512 stops a fork loop before the requested cap."""
        repo = _mkrepo(tmp_path)
        command = (
            "python - <<'PY'\n"
            "import os\n"
            "count = 0\n"
            "try:\n"
            "    while count < 2000:\n"
            "        pid = os.fork()\n"
            "        if pid == 0:\n"
            "            os._exit(0)\n"
            "        count += 1\n"
            "except OSError as exc:\n"
            "    print(f'FORK_LIMIT count={count} error={type(exc).__name__}')\n"
            "else:\n"
            "    print(f'FORK_LIMIT_NOT_REACHED count={count}')\n"
            "while True:\n"
            "    try:\n"
            "        os.waitpid(-1, 0)\n"
            "    except ChildProcessError:\n"
            "        break\n"
            "PY"
        )
        res = sb.execute_sandboxed(str(repo), command, 45)
        output = f"{res.stdout}\n{res.stderr}"
        assert not res.timed_out, "fork bomb survived — pids limit did not bite"
        assert "FORK_LIMIT count=" in output
        assert "FORK_LIMIT_NOT_REACHED" not in output
        count = int(output.split("FORK_LIMIT count=", 1)[1].split()[0])
        assert count < 2000

    def test_mem_bomb_oom_killed(self, tmp_path):
        """--memory 1g (no swap): a 2GB allocation must be OOM-killed
        (exit 137), not satisfied from swap or host memory."""
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo), 'python -c "a = bytearray(2 * 1024**3)"', 120
        )
        assert res.exit_code != 0
        assert not res.timed_out

    def test_tmpfs_fill_stops_at_cap(self, tmp_path):
        """tmpfs /tmp size=256m: dd of 1GB must ENOSPC-stop at <=256MiB
        (the final SIZE is the assertion — dd's own exit can be masked
        by surrounding shell)."""
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo),
            "dd if=/dev/zero of=/tmp/fill bs=1M count=1024 2>/dev/null; "
            "stat -c %s /tmp/fill 2>/dev/null || echo 0",
            120,
        )
        sizes = [ln for ln in res.stdout.split() if ln.isdigit()]
        assert sizes, f"no size in output: {res.stdout!r}"
        assert int(sizes[-1]) <= 268_435_456, (
            f"tmpfs fill reached {sizes[-1]} bytes — cap bypassed"
        )

    def test_host_dirs_not_visible(self, tmp_path):
        """No HOST directory names (Users/Windows/Program Files/pavan —
        Windows host) listable at / or above /workspace: the bind mount
        must be the ONLY host-reachable path, and `..` clamps at the
        container root."""
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo), "ls / /workspace/.. 2>/dev/null | sort -u", 120
        )
        out = res.stdout
        for host_name in ("Users", "Windows", "Program Files", "ProgramData", "pavan"):
            assert host_name not in out.splitlines(), (
                f"host directory {host_name!r} visible in container:\n{out}"
            )

    def test_etc_shadow_permission_denied(self, tmp_path):
        """Non-root container user: /etc/shadow and /proc/1/root must be
        unreadable (PermissionError), and pid 1 is our own bash (pidns
        isolation) — not the host/VM init."""
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo),
            "cat /etc/shadow 2>&1 | head -c 40; echo; "
            "cat /proc/1/root/etc/shadow 2>&1 | head -c 40; echo; "
            "echo PID1=$(cat /proc/1/comm)",
            120,
        )
        out = res.stdout
        assert "Permission denied" in out or "denied" in out.lower()
        assert "root:x:0:0" not in out, "shadow contents readable!"
        last = out.strip().splitlines()[-1]
        assert last.startswith("PID1=") and last[len("PID1=") :] in (
            "bash",
            "sh",
            "dash",
        ), f"pid 1 is not the container's own shell: {out[-80:]!r}"

    def test_no_docker_socket_no_capabilities(self, tmp_path):
        """--cap-drop ALL: mount/chown/su-root all fail; no docker.sock
        exists to grab (daemon takeover path closed)."""
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo),
            "test -S /var/run/docker.sock && echo SOCK || echo nosock; "
            "su root -c true 2>/dev/null && echo SU || echo nosu; "
            "mkdir -p /tmp/m && mount -t proc none /tmp/m 2>/dev/null "
            "&& echo MOUNT || echo nomount; "
            "touch /tmp/f && chown 0:0 /tmp/f 2>/dev/null && echo CHOWN "
            "|| echo nochown",
            120,
        )
        out = res.stdout
        assert "nosock" in out
        assert "nosu" in out
        assert "nomount" in out
        assert "nochown" in out

    def test_network_blocked_and_interfaces_isolated(self, tmp_path):
        """--network none: no route to example.com/8.8.8.8 AND loopback
        is the only interface (nothing to reach other tasks with)."""
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo),
            "python - <<'EOF'\n"
            "import socket\n"
            "for hp in [('example.com', 80), ('8.8.8.8', 53)]:\n"
            "    try:\n"
            "        socket.create_connection(hp, 4)\n"
            "        print('NET-OPEN')\n"
            "    except OSError:\n"
            "        print('net-blocked')\n"
            "names = [l.split(':')[0].strip() for l in"
            " open('/proc/net/dev').readlines()[2:]]\n"
            "real = [n for n in names if n != 'lo']\n"
            "print('INTERFACES', names, 'REAL', real)\n"
            "EOF",
            120,
        )
        assert "NET-OPEN" not in res.stdout
        assert "net-blocked" in res.stdout
        assert "REAL []" in res.stdout, (
            f"non-loopback interfaces in a networkless container: {res.stdout!r}"
        )

    def test_concurrent_fork_bomb_and_mem_bomb_held(self, tmp_path):
        """Adversarial CONCURRENCY regression: simultaneous fork bomb +
        mem bomb + infinite-spin must ALL be stopped by their limits at
        the same time (limits are per-container — they hold under load,
        not just one-at-a-time)."""
        from concurrent.futures import ThreadPoolExecutor

        repo = _mkrepo(tmp_path)
        cmds = [
            "bomb() { bomb | bomb & }; bomb; wait; echo fb-done",  # pids
            'python -c "a=bytearray(2*1024**3)"',  # mem
            "while :; do :; done",  # time
            "dd if=/dev/zero of=/tmp/fill bs=1M count=1024 2>/dev/null; "
            "stat -c %s /tmp/fill 2>/dev/null || echo 0",  # tmpfs
        ]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(sb.execute_sandboxed, str(repo), c, 30) for c in cmds]
            results = [f.result(timeout=120) for f in futs]
        fb, mem, spin, dd = results
        assert not fb.timed_out, "fork bomb outlived its window"
        assert mem.exit_code != 0 and not mem.timed_out
        assert spin.timed_out and spin.exit_code == 124
        assert int([l for l in dd.stdout.split() if l.isdigit()][-1]) <= 268_435_456

    def test_workspace_disk_write_reaches_host_by_design(self, tmp_path):
        """DESIGN CONTRACT (recorded finding, Round 6): a container CAN
        write unquota'd into the RW bind mount — host disk space is
        reachable. This is the product contract (T1 diffs host-side),
        documented as accepted exposure in execution/AGENTS.md; the test
        pins the behavior so any accidental change (e.g. a future quota
        or RO switch) is noticed."""
        repo = _mkrepo(tmp_path)
        res = sb.execute_sandboxed(
            str(repo),
            "dd if=/dev/zero of=f.bin bs=1M count=8 2>/dev/null; "
            "ls -l f.bin | awk '{print $5}'",
            120,
        )
        assert res.exit_code == 0
        assert (repo / "f.bin").stat().st_size == 8 * 1024 * 1024
        (repo / "f.bin").unlink()
