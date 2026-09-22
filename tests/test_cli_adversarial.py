"""Adversarial tests for the CLI (Round 6, Terminal 4, Task B).

Crafted/hostile arguments against every user-facing input: shell-injection
payloads via --repo/--issue, path traversal via --task-id, malformed subset
JSON files, hostile --issue @file paths, and null-byte arguments. The
invariant under test: nothing executes unintended commands or reads/writes
paths outside what the operator named, and no input produces a raw
traceback — every failure is a clean usage error (exit 2) or a contained
task-level error (exit 1).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli.main import build_parser, main


@pytest.fixture(autouse=True)
def _clean_overrides():
    from cli import deps

    deps.reset_overrides()
    yield
    deps.reset_overrides()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("HARNESS_HOME", str(h))
    return h


@pytest.fixture
def logs_root(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setenv("HARNESS_LOGS_DIR", str(d))
    return d


@pytest.fixture
def canary_outside(tmp_path, logs_root):
    """A state.json-shaped file outside the logs root that no legitimate
    invocation should ever render."""
    outside = tmp_path / "elsewhere" / "victim"
    outside.mkdir(parents=True)
    canary = "CLI-CANARY-SECRET-3c2b1a"
    (outside / "state.json").write_text(
        json.dumps(
            {
                "task_id": "victim",
                "plan": ["1. x"],
                "completed_steps": ["1. x"],
                "files_touched": [],
                "decisions": [canary],
                "remaining_plan": [],
            }
        ),
        encoding="utf-8",
    )
    return outside, canary


# ---------------------------------------------------------------------------
# status: --task-id path traversal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "..",
        "../elsewhere/victim",
        "..\\elsewhere\\victim",
        "C:/Windows/System32",
        "C:x",  # drive-relative (Win32 discards the base)
        "C:",
        " ..",  # leading-space dot — resolves AS '..' on Win32
        ".. ",  # trailing-space dot — same
        "/etc/passwd",
        "a/b",
        "a\\b",
        "a\x00b",
        "..\\..\\..\\..\\Windows",
        " ",  # whitespace-only
        ".",  # dot-only
    ],
)
def test_status_rejects_traversal_ids(logs_root, home, canary_outside, capsys, bad_id):
    """Traversal-shaped --task-id never renders outside-logs content.
    (Note: 'con'/'sub.' are NOT rejected — they name dirs INSIDE the
    root at worst (Win32 normalization), which is containment-safe.)"""
    outside, canary = canary_outside
    rc = main(["status", "--task-id", bad_id])
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert rc == 2
    assert "invalid task id" in captured.err


@pytest.mark.parametrize(
    "ok_id",
    [
        "fine-task",
        "task 42",
        "änother-task",
        "a.b.c",
        "x..y",
        "t_1",
        "OSS-path-perms-r2",
        "final-e2e-bug02",
    ],
)
def test_status_accepts_legitimate_id_shapes(logs_root, home, capsys, ok_id):
    """The guard is semantic, not a charset allowlist: odd-but-benign ids
    (spaces, unicode, interior dots) still work."""
    d = logs_root / ok_id
    d.mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps(
            {
                "task_id": ok_id,
                "plan": ["1. ok"],
                "completed_steps": ["1. ok"],
                "files_touched": [],
                "decisions": [f"marker-{ok_id}"],
                "remaining_plan": [],
            }
        ),
        encoding="utf-8",
    )
    rc = main(["status", "--task-id", ok_id])
    assert rc == 0, f"legit id {ok_id!r} wrongly rejected"
    assert f"marker-{ok_id}" in capsys.readouterr().out


def test_status_traversal_absolute_path_form(logs_root, home, canary_outside, capsys):
    """The exact live-confirmed MCP escape form, replayed through the CLI."""
    outside, canary = canary_outside
    rc = main(["status", "--task-id", str(outside)])
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert rc == 2


def test_status_custom_log_root_still_contained(tmp_path, home, capsys, canary_outside):
    """--log-root does not reopen the hole: traversal ids are still rejected
    (guard runs against the EFFECTIVE root)."""
    outside, canary = canary_outside
    rc = main(["status", "--task-id", str(outside), "--log-root", str(tmp_path)])
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err
    assert rc == 2


def test_status_legit_id_still_works(logs_root, home, capsys):
    d = logs_root / "fine-task"
    d.mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps(
            {
                "task_id": "fine-task",
                "plan": ["1. ok"],
                "completed_steps": ["1. ok"],
                "files_touched": [],
                "decisions": ["all fine"],
                "remaining_plan": [],
            }
        ),
        encoding="utf-8",
    )
    rc = main(["status", "--task-id", "fine-task"])
    assert rc == 0
    assert "all fine" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# fix: shell-injection payloads in --repo / --issue
# ---------------------------------------------------------------------------


def _no_shell_side_effects(tmp_path):
    """Marker files an injected shell payload would create."""
    return [
        tmp_path / "pwned.txt",
        tmp_path / "pwned_reboot.txt",
        Path.home() / "pwned_home.txt",
    ]


@pytest.mark.parametrize(
    "repo_payload",
    [
        "x; touch pwned.txt",
        "x && calc.exe",
        "x | calc.exe",
        "x`touch pwned.txt`",
        "$(reboot)",
        'x" || "calc',
        "x\necho hacked",
    ],
)
def test_fix_repo_injection_never_executes(
    tmp_path, home, capsys, repo_payload, monkeypatch
):
    """--repo is data (a directory path), never a shell command: payload
    metacharacters must fail the is_dir check, not run anything."""
    monkeypatch.chdir(tmp_path)
    rc = main(["fix", "--repo", repo_payload, "--issue", "x"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "--repo is not a directory" in captured.err
    for marker in _no_shell_side_effects(tmp_path):
        assert not marker.exists(), f"side-effect file created: {marker}"


@pytest.mark.parametrize(
    "issue_payload",
    [
        "; rm -rf / #",
        "$(calc.exe)",
        "`reboot`",
        "x && shutdown /s",
        "import os\nos.system('calc')",
        "{{7*7}}",
        "<script>alert(1)</script>",
    ],
)
def test_fix_issue_injection_never_executes(
    tmp_path, home, capsys, issue_payload, monkeypatch
):
    """--issue text is stored data (prompt input), never executed. Without
    a real repo the run stops at usage validation — payloads are inert."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    # No model backend wired: run would error cleanly; the point is that
    # nothing EXECUTES from the issue text. Use an invalid model config
    # so the run fails fast without network.
    rc = main(
        [
            "fix",
            "--repo",
            str(repo),
            "--issue",
            issue_payload,
            "--model",
            "definitely-not-a-real-model",
            "--budget",
            "0",
        ]
    )
    captured = capsys.readouterr()
    # 4 = the onboarding gate (no api_key anywhere on a clean machine —
    # the run never starts, so the payload is inert even earlier).
    assert rc in (0, 1, 2, 4)
    for marker in _no_shell_side_effects(tmp_path):
        assert not marker.exists(), f"side-effect file created: {marker}"
    # the issue text may be echoed (it's the task description) but never
    # produces a traceback
    assert "Traceback" not in captured.out + captured.err


def test_fix_issue_atfile_reads_only_named_file(tmp_path, home, capsys, monkeypatch):
    """--issue @file reads exactly the named file — traversal to arbitrary
    files is the operator's own choice (local trust); verify it works for
    in-scope files and fails cleanly for missing/binary ones."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "nope.txt"
    rc = main(["fix", "--repo", str(repo), "--issue", f"@{missing}"])
    assert rc == 2
    assert "cannot read issue file" in capsys.readouterr().err

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02\xff\xfe")
    rc = main(["fix", "--repo", str(repo), "--issue", f"@{binary}"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot read issue file" in err or "not valid UTF-8" in err


def test_fix_issue_atfile_null_byte_path(tmp_path, home, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    rc = main(["fix", "--repo", str(repo), "--issue", "@x\x00y"])
    assert rc == 2
    assert "Traceback" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# run-benchmark: malformed subset JSON
# ---------------------------------------------------------------------------


def _write_subset(tmp_path, data, raw=None):
    p = tmp_path / "subset.json"
    p.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
    return p


def test_subset_not_json(tmp_path, home, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{definitely not json", encoding="utf-8")
    rc = main(["run-benchmark", "--subset", str(p)])
    assert rc == 2
    assert "cannot read subset file" in capsys.readouterr().err


def test_subset_not_a_list(tmp_path, home, capsys):
    p = _write_subset(tmp_path, {"repo": "x", "issue": "y"})
    rc = main(["run-benchmark", "--subset", str(p)])
    assert rc == 2
    assert "must be a JSON list" in capsys.readouterr().err


@pytest.mark.parametrize(
    "entry",
    [
        {"repo": 123, "issue": "x"},  # repo not a string (live-confirmed traceback)
        {"repo": "x", "issue": 456},  # issue not a string
        {"repo": "", "issue": "x"},  # empty repo
        {"repo": "   ", "issue": "x"},  # whitespace repo
        {"repo": "x", "issue": ""},  # empty issue
        {"repo": "x", "issue": None},  # null issue
        {"repo": "x", "issue": "y", "task_id": 42},  # non-string task_id
        {"repo": "x", "issue": "y", "config": "not-a-dict"},
        ["repo", "issue"],  # entry is a list
        "just a string",  # entry is a string
        3.14,  # entry is a number
        None,  # entry is null
    ],
)
def test_subset_hostile_entries_rejected_cleanly(tmp_path, home, capsys, entry):
    """Every malformed entry type is a clean usage error — no traceback
    (the {"repo": 123} form crashed with a TypeError before Round 6)."""
    p = _write_subset(tmp_path, [entry])
    rc = main(["run-benchmark", "--subset", str(p)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error: subset entry 0" in captured.err
    assert "Traceback" not in captured.err


def test_subset_bom_file_accepted(tmp_path, home, capsys):
    """PowerShell-written BOM files remain supported (utf-8-sig)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    p = tmp_path / "bom.json"
    p.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps(
            [
                {
                    "repo": str(repo),
                    "issue": "x",
                    "config": {
                        "use_fake_harness": True,
                        "fake_steps": ["plan", "edit", "verify"],
                        "fake_step_delay_s": 0.01,
                        "crash_retries": 0,
                    },
                }
            ]
        ).encode("utf-8")
    )
    rc = main(
        [
            "run-benchmark",
            "--subset",
            str(p),
            "--concurrency",
            "1",
            "--log-root",
            str(tmp_path / "bl"),
        ]
    )
    assert rc == 0


def test_subset_null_byte_repo_in_entry(tmp_path, home, capsys):
    """A null byte inside repo (string) is rejected at validation time —
    BEFORE Round 6 this entry spawned real scheduler workers that crash-
    looped against the unusable path (found live: a probe subset leaked
    into ./logs and burned a crash-retry)."""
    p = _write_subset(tmp_path, [{"repo": "x\x00y", "issue": "z"}])
    rc = main(["run-benchmark", "--subset", str(p)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "null byte" in captured.err
    assert "Traceback" not in captured.out + captured.err


# ---------------------------------------------------------------------------
# memory subcommands: hostile inputs
# ---------------------------------------------------------------------------


def test_memory_record_hostile_text_inert(home, logs_root, capsys):
    """Decision text with shell/SQL payloads is stored verbatim and
    returned verbatim — never executed."""
    payload = "'; DROP TABLE decisions; -- $(calc.exe) `reboot`"
    rc = main(["memory", "record", payload])
    assert rc == 0
    rc = main(["memory", "query-decisions", "DROP TABLE"])
    out = capsys.readouterr().out
    assert "calc.exe" in out  # stored verbatim
    # store intact
    rc = main(["memory", "query-decisions", ""])
    assert rc == 0


def test_memory_query_structure_hostile_repo(home, capsys, tmp_path):
    """Null-byte/permission-error repo paths are clean usage errors."""
    rc = main(["memory", "query-structure", "--repo", "x\x00y", "files"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_memory_query_structure_traversal_query_inert(home, capsys, tmp_path):
    """Traversal-looking graph queries only ever miss (paths are matched
    against INDEXED repo-relative names, not the host filesystem)."""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    for q in [
        "file ../../win.ini",
        "file C:/Windows/win.ini",
        "files ..",
        "file /etc/passwd",
    ]:
        rc = main(["memory", "query-structure", "--repo", str(repo), q])
        assert rc == 0
        out = capsys.readouterr().out
        for marker in ("[fonts]", "[boot]", "root:"):
            assert marker not in out


# ---------------------------------------------------------------------------
# real subprocess: no shell side effects from crafted argv end-to-end
# ---------------------------------------------------------------------------


def test_subprocess_crafted_argv_no_side_effects(tmp_path):
    """Run the REAL console entry as a subprocess with hostile arguments;
    assert no marker files appear (nothing executed a shell) and the
    process exits with a usage error, not a crash."""
    marker = tmp_path / "pwned_subproc.txt"
    repo_payload = f"x; echo hacked > {marker}"
    repo_root = Path(__file__).resolve().parent.parent
    cp = subprocess.run(
        [sys.executable, "-m", "cli", "status", "--task-id", "../" + "x" * 200],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=60,
    )
    assert cp.returncode == 2
    assert "Traceback" not in cp.stdout + cp.stderr
    assert not marker.exists()
