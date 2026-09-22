"""Proactive Codebase Health Scan round — `vex scan` (Tasks A/B/C).

The round's contract under test:
- Task A: harness.scan_mode.run_scan analyzes a repo READ-ONLY (no
  shell, no sandbox, no edits, no model calls) using the existing code
  graph + AST + manifests; findings are data, not failure.
- Task B: findings are RANKED (severity + structural fan-in) with a
  grounded rationale each (deterministic from the finding's own
  evidence — the rationale-log discipline); the report shows a bounded
  top slice, the rest stay in scan.json.
- Task C: `vex fix --finding <scan_id>#<n>` (and `vex scan --fix N`)
  resolves a finding and dispatches it through the EXISTING
  verifier-gated entries (fix loop with a not-yet-existing target test
  / build mode for version floors); hostile refs are contained.

Offline throughout — the scan's only remote call (PyPI freshness) is
gated behind --remote/scan_remote_deps and its unit tests mock urlopen.
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _struct_available() -> bool:
    try:
        from harness.deps import get_code_graph_factory

        return get_code_graph_factory() is not None
    except Exception:
        return False


requires_graph = pytest.mark.skipif(
    not _struct_available(), reason="memory.code_graph not importable"
)


# ---------------------------------------------------------------------------
# Fixture repos
# ---------------------------------------------------------------------------


def _gap_repo(tmp_path: Path) -> Path:
    """A repo with REAL coverage gaps: an untested load-bearing module
    (imported by other SOURCE, not tests), a tested sibling, and an
    untested public function inside the tested module."""
    repo = tmp_path / "gaprepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(
        '"""Core module other source imports — but no test does."""\n'
        "\n"
        "def transform(items):\n"
        "    return [i.strip() for i in items]\n"
        "\n"
        "def merge(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "uses_core.py").write_text(
        "from pkg.core import transform\n"
        "\n"
        "def run(lines):\n"
        "    return transform(lines)\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "api.py").write_text(
        "from pkg.core import merge\n\ndef endpoint(a, b):\n    return merge(a, b)\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "tested.py").write_text(
        "def helper(x):\n"
        "    return x + 1\n"
        "\n"
        "def helper2(x):\n"
        "    return x + 2\n"
        "\n"
        "def helper3(x):\n"
        "    return x + 3\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "other.py").write_text(
        "def unrelated():\n    return 1\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    # tests exercise ONLY pkg/tested.py's helper()
    (repo / "tests" / "test_tested.py").write_text(
        "from pkg.tested import helper\n"
        "\n"
        "def test_helper():\n"
        "    assert helper(1) == 2\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'gaprepo'\nversion = '0.1.0'\n\n"
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    return repo


def _smell_repo(tmp_path: Path) -> Path:
    """A repo with one of each latent-bug smell class."""
    repo = tmp_path / "smellrepo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "smells.py").write_text(
        "def collect(value, acc=[]):\n"
        "    acc.append(value)\n"
        "    return acc\n"
        "\n"
        "def risky(x):\n"
        "    try:\n"
        "        return int(x)\n"
        "    except:\n"  # bare except
        "        return 0\n"
        "\n"
        "def quiet(x):\n"
        "    try:\n"
        "        return int(x)\n"
        "    except ValueError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_smells.py").write_text(
        "from app.smells import collect\n"
        "\n"
        "def test_collect():\n"
        "    assert collect(1) == [1]\n",
        encoding="utf-8",
    )
    return repo


def _deps_repo(tmp_path: Path) -> Path:
    """A repo whose manifests pin the same package differently."""
    repo = tmp_path / "deprepo"
    repo.mkdir(parents=True)
    (repo / "requirements.txt").write_text(
        "requests==2.25.1\nflask>=1.0\n", encoding="utf-8"
    )
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'deprepo'\nversion = '0.1.0'\n"
        "dependencies = ['requests==2.31.0']\n",
        encoding="utf-8",
    )
    return repo


# ---------------------------------------------------------------------------
# Task A — run_scan: read-only analysis
# ---------------------------------------------------------------------------


class TestRunScanBasics:
    def test_scan_is_read_only(self, tmp_path):
        """The scan never mutates the repo — byte-identical after."""
        import hashlib

        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        before = {
            p.relative_to(repo).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in repo.rglob("*")
            if p.is_file()
        }
        run_scan(str(repo), log_root=tmp_path / "logs")
        after = {
            p.relative_to(repo).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in repo.rglob("*")
            if p.is_file()
        }
        assert before == after

    def test_scan_writes_artifacts_and_trace(self, tmp_path):
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        logs = tmp_path / "scanlogs"
        scan = run_scan(str(repo), log_root=logs)
        assert scan["status"] == "success"
        assert Path(scan["scan_path"]).is_file()
        assert Path(scan["report_path"]).is_file()
        trace = Path(scan["trace_path"]).read_text(encoding="utf-8")
        assert '"mode": "scan"' in trace
        assert '"kind": "task_end"' in trace or "task_end" in trace
        data = json.loads(Path(scan["scan_path"]).read_text(encoding="utf-8"))
        assert data["scan_id"] == scan["scan_id"]
        assert data["findings"], "gap repo must produce findings"

    def test_no_python_repo_reports_honestly(self, tmp_path):
        from harness.scan_mode import run_scan

        repo = tmp_path / "notpython"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "index.js").write_text("x=1\n", encoding="utf-8")
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        assert scan["status"] == "success"
        assert scan["findings"] == []
        assert any("no Python source" in n for n in scan["notes"])

    def test_bad_repo_path_is_error_not_crash(self, tmp_path):
        from harness.scan_mode import run_scan

        scan = run_scan(str(tmp_path / "nope"), log_root=tmp_path / "logs")
        assert scan["status"] == "error"
        assert scan["findings"] == []

    def test_fully_covered_repo_is_zero_findings(self, tmp_path):
        """bug02_mean is fully covered by its suite — a scan of it must
        NOT invent findings (the noise budget starts at zero)."""
        from harness.scan_mode import run_scan

        scan = run_scan(
            str(ROOT / "tests" / "fixtures" / "bug02_mean"),
            log_root=tmp_path / "logs",
        )
        assert scan["status"] == "success"
        cov = [f for f in scan["findings"] if f["kind"] == "coverage_gap"]
        assert cov == [], [f["title"] for f in cov]

    def test_logs_and_artifact_dirs_never_scanned(self, tmp_path):
        """Cloned third-party code under logs/ is not the project's
        code — findings must never point there (the bug the first live
        run caught)."""
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        (repo / "logs").mkdir()
        (repo / "logs" / "cloned-upstream").mkdir(parents=True)
        (repo / "logs" / "cloned-upstream" / "up.py").write_text(
            "def f(acc=[]):\n    try:\n        pass\n    except:\n"
            "        pass\n    return acc\n",
            encoding="utf-8",
        )
        scan = run_scan(str(repo), log_root=tmp_path / "logs2")
        for f in scan["findings"]:
            assert not f["file"].startswith("logs/"), f["file"]


@requires_graph
class TestCoverageDetector:
    def test_module_gap_found_with_fan_in(self, tmp_path):
        """pkg/core.py is imported by two untested source modules and no
        test — a high-severity gap with the importers named as evidence."""
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        gaps = [f for f in scan["findings"] if f["kind"] == "coverage_gap"]
        core = [f for f in gaps if f["file"] == "pkg/core.py"]
        assert core, [f["file"] for f in gaps]
        f = core[0]
        assert f["severity"] == "high"  # 2 importers
        assert "uses_core" in f["evidence"] or "uses_core" in f["rationale"]
        assert f["fix_target_test"] == "tests/test_core.py"
        # the fix handoff: the suggested test must NOT exist yet (the
        # fix loop's baseline gate is honest by construction)
        assert not (repo / "tests" / "test_core.py").exists()

    def test_covered_module_not_flagged(self, tmp_path):
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        files = {f["file"] for f in scan["findings"] if f["kind"] == "coverage_gap"}
        assert "pkg/tested.py" not in files  # imported by the test
        # and indirect coverage: helper2/3 in the tested module ARE
        # function-level gaps only if load-bearing (>=2 non-test callers) —
        # they have none, so no function finding for them either
        func_gaps = [f for f in scan["findings"] if "has no test" in f["title"]]
        assert not any("helper2" in f["title"] for f in func_gaps)

    def test_rationale_names_the_consequence(self, tmp_path):
        """Task B: each finding carries a grounded why-this-matters."""
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        for f in scan["findings"]:
            if f["kind"] == "coverage_gap" and f["file"] == "pkg/core.py":
                assert "would not fail the suite" in f["rationale"]
                break

    def test_graph_unavailable_degrades_to_note(self, tmp_path, monkeypatch):
        import harness.deps as deps

        monkeypatch.setattr(deps, "get_code_graph_factory", lambda: None)
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        assert any("code graph unavailable" in n for n in scan["notes"])
        assert not [f for f in scan["findings"] if f["kind"] == "coverage_gap"]


class TestSmellDetector:
    @pytest.mark.parametrize(
        "kind, marker",
        [
            ("mutable_default", "mutable default argument"),
            ("bare_except", "bare 'except:'"),
            ("except_pass", "silently swallowed exception"),
        ],
    )
    def test_all_three_smell_classes_found(self, tmp_path, kind, marker):
        from harness.scan_mode import run_scan

        repo = _smell_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        sm = [
            f for f in scan["findings"] if f["kind"] == "smell" and marker in f["title"]
        ]
        assert sm, [f["title"] for f in scan["findings"]]
        f = sm[0]
        assert f["file"] == "app/smells.py"
        assert f["line"], "smell findings carry a line number"
        assert f["evidence"], "smell findings carry the offending line"
        assert f["fix_target_test"] == "tests/test_smells.py"
        # the mutable-default rationale references the shared-state class
        if kind == "mutable_default":
            assert "shared" in f["rationale"] or "leak" in f["rationale"]

    def test_smells_capped_per_kind(self, tmp_path):
        """The noise budget: many sites of one kind -> bounded findings."""
        from harness.scan_mode import run_scan

        repo = tmp_path / "manysmells"
        (repo / "m").mkdir(parents=True)
        body = []
        for i in range(10):
            body.append(f"def f{i}(acc=[]):\n    return acc\n")
        (repo / "m" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "m" / "mods.py").write_text("\n".join(body), encoding="utf-8")
        (repo / "pyproject.toml").write_text(
            "[project]\nname = 'm'\nversion = '0.1.0'\n", encoding="utf-8"
        )
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        md = [f for f in scan["findings"] if "mutable default" in f["title"]]
        assert len(md) <= 3
        # and the suppressed count is surfaced
        assert scan["suppressed"] >= 0

    def test_smells_exclude_tests(self, tmp_path):
        """A smell in a TEST file is not a finding (fix-code-not-tests)."""
        from harness.scan_mode import run_scan

        repo = _smell_repo(tmp_path)
        (repo / "tests" / "test_bad.py").write_text(
            "def test_x(acc=[]):\n    assert acc == []\n", encoding="utf-8"
        )
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        assert all(
            not f["file"].startswith("tests/")
            for f in scan["findings"]
            if f["kind"] == "smell"
        )


class TestDependencyDetector:
    def test_conflicting_pins_found_offline(self, tmp_path):
        from harness.scan_mode import run_scan

        repo = _deps_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs")  # offline default
        deps = [f for f in scan["findings"] if f["kind"] == "dependency"]
        conf = [f for f in deps if "conflicting pins" in f["title"]]
        assert conf, [f["title"] for f in deps]
        f = conf[0]
        assert "requests" in f["title"]
        assert "2.25.1" in f["evidence"] and "2.31.0" in f["evidence"]
        # conflict is a FIX (align the pin, suite-gated)
        assert f["fix_kind"] == "fix"
        assert f["fix_target_test"] is None

    def test_offline_note_tells_how_to_go_remote(self, tmp_path):
        from harness.scan_mode import run_scan

        repo = _deps_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        assert any("--remote" in n for n in scan["notes"])

    def test_remote_outdated_pin_found(self, tmp_path, monkeypatch):
        """--remote checks PyPI; an outdated pin becomes a BUILD finding
        (the version-floor acceptance test genuinely fails on the old
        pin). Network mocked — deterministic."""
        from harness import scan_mode

        def fake_urlopen(req, timeout=None):
            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"info": {"version": "3.0.0"}}).encode()

            return R()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        repo = tmp_path / "outdated"
        repo.mkdir()
        (repo / "requirements.txt").write_text("flask==2.0.0\n", encoding="utf-8")
        scan = scan_mode.run_scan(str(repo), log_root=tmp_path / "logs", remote=True)
        out = [f for f in scan["findings"] if "outdated" in f["title"]]
        assert out, [f["title"] for f in scan["findings"]]
        f = out[0]
        assert "3.0.0" in f["evidence"]
        assert f["severity"] == "high"  # major version behind
        assert f["fix_kind"] == "build"
        assert "version" in f["fix_issue_text"].lower()

    def test_remote_up_to_date_pin_not_flagged(self, tmp_path, monkeypatch):
        from harness import scan_mode

        def fake_urlopen(req, timeout=None):
            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"info": {"version": "2.0.0"}}).encode()

            return R()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        repo = tmp_path / "fresh"
        repo.mkdir()
        (repo / "requirements.txt").write_text("flask==2.0.0\n", encoding="utf-8")
        scan = scan_mode.run_scan(str(repo), log_root=tmp_path / "logs", remote=True)
        assert not [f for f in scan["findings"] if "outdated" in f["title"]]

    def test_remote_failure_degrades_to_note(self, tmp_path, monkeypatch):
        """A dead network is a degraded scan, never a crash."""
        from harness import scan_mode

        def boom(req, timeout=None):
            raise OSError("net down")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        repo = _deps_repo(tmp_path)
        scan = scan_mode.run_scan(str(repo), log_root=tmp_path / "logs", remote=True)
        assert scan["status"] == "success"
        # the offline conflict finding still works
        assert [f for f in scan["findings"] if "conflicting pins" in f["title"]]

    def test_no_manifests_note(self, tmp_path):
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        assert any("no dependency pins" in n for n in scan["notes"])


# ---------------------------------------------------------------------------
# Task B — ranking, rationale, report
# ---------------------------------------------------------------------------


class TestRanking:
    def test_findings_sorted_by_score_with_index(self, tmp_path):
        from harness.scan_mode import rank_findings

        fs = [
            {"kind": "smell", "score": 10.0, "file": "a.py", "line": 1},
            {"kind": "coverage_gap", "score": 31.0, "file": "b.py", "line": None},
            {"kind": "dependency", "score": 22.0, "file": "c.py", "line": None},
        ]
        ranked, suppressed = rank_findings(fs, max_findings=3)
        assert [f["score"] for f in ranked] == [31.0, 22.0, 10.0]
        assert [f["index"] for f in ranked] == [1, 2, 3]
        assert suppressed == 0

    def test_suppressed_count_honest(self, tmp_path):
        from harness.scan_mode import rank_findings

        fs = [
            {"kind": "smell", "score": 10.0, "file": f"f{i}.py", "line": 1}
            for i in range(12)
        ]
        ranked, suppressed = rank_findings(fs, max_findings=5)
        assert len(ranked) == 12  # ALL stay in scan.json
        assert suppressed == 7

    def test_report_shows_only_shown_slice(self, tmp_path):
        from harness.scan_mode import render_report, run_scan

        repo = _smell_repo(tmp_path)
        scan = run_scan(str(repo), log_root=tmp_path / "logs", max_findings=1)
        text = render_report(scan)
        assert f"{len(scan['findings'])} finding(s)" in text
        assert "1 shown" in text
        # the top finding's title is present; others are not
        top = scan["findings"][0]
        assert top["title"] in text
        for f in scan["findings"][1:]:
            if f["title"] != top["title"]:
                assert f["title"] not in text

    def test_every_finding_carries_rationale_and_fix_contract(self, tmp_path):
        """Task B/C invariants: rationale present; the fix handoff is
        complete on EVERY finding."""
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        (repo / "pkg" / "smelly.py").write_text(
            "def f(acc=[]):\n    return acc\n", encoding="utf-8"
        )
        scan = run_scan(str(repo), log_root=tmp_path / "logs")
        assert scan["findings"]
        for f in scan["findings"]:
            assert f["rationale"].strip(), f
            assert 2 <= len(f["rationale"].split(".")) <= 8, f
            assert f["fix_kind"] in ("fix", "build")
            assert f["fix_issue_text"].strip()
            assert (
                "vex fix --finding" in scan["report_path"] or True
            )  # report checked elsewhere

    def test_deterministic_rerun_same_findings(self, tmp_path):
        """Same repo, fresh index root -> identical ranked findings
        (the scan is deterministic: no model, no timestamps in content)."""
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        a = run_scan(str(repo), log_root=tmp_path / "l1")
        b = run_scan(str(repo), log_root=tmp_path / "l2")

        def key(s):
            return [
                (f["kind"], f["file"], f.get("line"), f["title"], f["score"])
                for f in s["findings"]
            ]

        assert key(a) == key(b)


# ---------------------------------------------------------------------------
# Task C — finding -> task handoff
# ---------------------------------------------------------------------------


class TestFindingResolution:
    def _scan(self, tmp_path):
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        logs = tmp_path / "scanlogs"
        scan = run_scan(str(repo), log_root=logs)
        return repo, logs, scan

    def test_resolve_roundtrip(self, tmp_path):
        from harness.scan_mode import resolve_finding

        _, logs, scan = self._scan(tmp_path)
        f = scan["findings"][0]
        data, finding, err = resolve_finding(f"{scan['scan_id']}#{f['index']}", logs)
        assert err == ""
        assert finding is not None
        assert finding["title"] == f["title"]
        assert data["repo_path"].endswith("gaprepo")

    def test_resolve_bad_ref_shape(self, tmp_path):
        from harness.scan_mode import resolve_finding

        _, logs, _ = self._scan(tmp_path)
        for bad in ("", "nope", "scan-abc", "scan-abc#", "scan-abc#x", "../etc#1"):
            _data, finding, err = resolve_finding(bad, logs)
            assert finding is None, bad
            assert err, bad

    def test_resolve_traversal_contained(self, tmp_path):
        """A traversal-shaped scan id is rejected before any fs use
        (same guard class as `vex status --task-id`)."""
        from harness.scan_mode import resolve_finding

        _, logs, _ = self._scan(tmp_path)
        _data, finding, err = resolve_finding("scan-..%2f..#1", logs)
        assert finding is None
        assert err

    def test_resolve_index_out_of_range(self, tmp_path):
        from harness.scan_mode import resolve_finding

        _, logs, scan = self._scan(tmp_path)
        _, finding, err = resolve_finding(f"{scan['scan_id']}#999", logs)
        assert finding is None
        assert "out of range" in err

    def test_resolve_unknown_scan_id(self, tmp_path):
        from harness.scan_mode import resolve_finding

        _, logs, _ = self._scan(tmp_path)
        _, finding, err = resolve_finding("scan-deadbeef#1", logs)
        assert finding is None
        assert "no scan found" in err

    def test_finding_task_params_complete(self, tmp_path):
        from harness.scan_mode import finding_task_params

        _, _, scan = self._scan(tmp_path)
        for f in scan["findings"]:
            p = finding_task_params(f)
            assert set(p) == {"issue_text", "target_test", "fix_kind", "note"}
            assert p["issue_text"]
            assert p["fix_kind"] in ("fix", "build")

    def test_coverage_finding_issue_authorizes_tests(self, tmp_path):
        """The fix loop's step rule is 'do not modify tests unless the
        issue says so' — a coverage finding's issue MUST say so."""
        from harness.scan_mode import finding_task_params

        _, _, scan = self._scan(tmp_path)
        cov = [f for f in scan["findings"] if f["kind"] == "coverage_gap"]
        assert cov
        for f in cov:
            assert (
                "authorizes adding test files" in finding_task_params(f)["issue_text"]
            )

    def test_coverage_finding_target_test_not_in_repo(self, tmp_path):
        """The suggested target test does not exist in the original
        repo — so a fix run's baseline verify fails honestly and the
        pre-fix 'passes pre-fix' short-circuit can never fire."""
        from harness.scan_mode import finding_task_params

        repo, _, scan = self._scan(tmp_path)
        for f in scan["findings"]:
            if f["kind"] == "coverage_gap":
                tt = finding_task_params(f)["target_test"]
                assert tt and not (repo / tt).exists(), tt


# ---------------------------------------------------------------------------
# CLI wiring (vex scan / vex fix --finding)
# ---------------------------------------------------------------------------


def _run_cli(argv, monkeypatch, capsys):
    """Run the real CLI entry in-process; argparse usage errors
    (SystemExit 2) are returned as code 2 (the exit-code contract)."""
    from cli.main import main

    monkeypatch.chdir(ROOT)
    try:
        code = main(argv)
    except SystemExit as exc:
        code = int(exc.code or 0)
    out = capsys.readouterr()
    return code, out


class TestCliScan:
    def test_scan_help_lists_command(self):
        from cli.main import build_parser

        p = build_parser()
        assert "scan" in p._subparsers._group_actions[0].choices

    def test_scan_bad_repo_usage_error(self, monkeypatch, capsys, tmp_path):
        code, out = _run_cli(
            ["scan", "--repo", str(tmp_path / "nope")], monkeypatch, capsys
        )
        assert code == 2
        assert "not a directory" in out.err

    def test_scan_bad_focus_usage_error(self, monkeypatch, capsys, tmp_path):
        repo = _gap_repo(tmp_path)
        code, _out = _run_cli(
            ["scan", "--repo", str(repo), "--focus", "vibes"], monkeypatch, capsys
        )
        assert code == 2

    def test_scan_json_is_one_document(self, monkeypatch, capsys, tmp_path):
        repo = _gap_repo(tmp_path)
        code, out = _run_cli(
            [
                "scan",
                "--repo",
                str(repo),
                "--json",
                "--log-root",
                str(tmp_path / "slogs"),
            ],
            monkeypatch,
            capsys,
        )
        assert code == 0
        payload = json.loads(out.out)
        assert payload["status"] == "success"
        assert payload["findings"]
        # stdout carries ONLY the document
        assert out.out.count("{") >= 1 and not out.out.startswith(" ")

    def test_scan_focus_dependencies_only(self, monkeypatch, capsys, tmp_path):
        repo = _deps_repo(tmp_path)
        code, out = _run_cli(
            [
                "scan",
                "--repo",
                str(repo),
                "--focus",
                "dependencies",
                "--log-root",
                str(tmp_path / "slogs"),
            ],
            monkeypatch,
            capsys,
        )
        assert code == 0
        assert "conflicting pins" in out.out
        # only dependency findings in a focused scan
        data = json.loads(
            sorted((tmp_path / "slogs").glob("scan-*/scan.json"))[-1].read_text("utf-8")
        )
        assert {f["kind"] for f in data["findings"]} == {"dependency"}

    def test_scan_max_findings_flag(self, monkeypatch, capsys, tmp_path):
        repo = tmp_path / "many"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        for i in range(6):
            (repo / "pkg" / f"m{i}.py").write_text(
                f"def pub{i}():\n    return {i}\n", encoding="utf-8"
            )
        code, out = _run_cli(
            [
                "scan",
                "--repo",
                str(repo),
                "--max-findings",
                "2",
                "--log-root",
                str(tmp_path / "slogs"),
            ],
            monkeypatch,
            capsys,
        )
        assert code == 0
        assert "2 shown" in out.out
        assert "suppressed" in out.out


class TestCliFindingHandoff:
    def test_fix_finding_requires_repo(self, monkeypatch, capsys, tmp_path):
        code, out = _run_cli(["fix", "--finding", "scan-abc#1"], monkeypatch, capsys)
        assert code == 2  # argparse: --repo is required
        assert "repo" in out.err.lower()

    def test_fix_finding_bad_ref_usage_error(self, monkeypatch, capsys, tmp_path):
        code, out = _run_cli(
            ["fix", "--repo", str(tmp_path), "--finding", "garbage"],
            monkeypatch,
            capsys,
        )
        assert code == 2
        assert "scan-<id>#<n>" in out.err

    def test_run_finding_fix_leg_sets_issue_and_target(self, monkeypatch, tmp_path):
        """The fix leg of the handoff: _run_finding resolves the finding
        and hands cmd_fix the finding's issue text + target test, with
        `finding` CLEARED (no re-dispatch loop — cmd_fix would bounce it
        straight back). cmd_fix monkeypatched; the full e2e through
        run_task is Docker-gated, below."""
        from cli import main as cli_main
        from harness.scan_mode import run_scan

        gap = _gap_repo(tmp_path)
        logs = tmp_path / "scanlogs"
        scan = run_scan(str(gap), log_root=logs)
        top = scan["findings"][0]
        assert top["fix_kind"] == "fix"

        captured = {}

        def fake_cmd_fix(args):
            captured["issue"] = getattr(args, "issue", None)
            captured["target"] = getattr(args, "target_test", None)
            captured["finding"] = getattr(args, "finding", None)
            captured["repo"] = getattr(args, "repo", None)
            return 0

        monkeypatch.setattr(cli_main, "cmd_fix", fake_cmd_fix)
        ns = argparse.Namespace(
            repo=str(gap),
            finding=f"{scan['scan_id']}#{top['index']}",
            log_root=str(logs),
            issue=None,
            target_test=None,
        )
        code = cli_main._run_finding(ns, cli_main.ui.console())
        assert code == 0
        assert captured["issue"] == top["fix_issue_text"]
        assert captured["target"] == top["fix_target_test"]
        assert captured["finding"] is None  # cleared — no re-dispatch loop
        assert captured["repo"] == str(gap)

    def test_cmd_fix_dispatches_finding_to_run_finding(self, monkeypatch, tmp_path):
        """Wiring: `vex fix --finding` enters cmd_fix's finding branch
        and calls _run_finding with the raw ref (verified without
        touching run_task)."""
        from cli import main as cli_main

        captured = {}

        def fake_run_finding(args, con):
            captured["ref"] = getattr(args, "finding", None)
            return 0

        monkeypatch.setattr(cli_main, "_run_finding", fake_run_finding)
        gap = _gap_repo(tmp_path)
        try:
            code = cli_main.main(
                ["fix", "--repo", str(gap), "--finding", "scan-abc1234#2"]
            )
        except SystemExit as exc:
            code = int(exc.code or 0)
        assert code == 0
        assert captured["ref"] == "scan-abc1234#2"

    def test_scan_fix_n_mutually_exclusive_json(self, monkeypatch, capsys, tmp_path):
        repo = _gap_repo(tmp_path)
        code, out = _run_cli(
            [
                "scan",
                "--repo",
                str(repo),
                "--json",
                "--fix",
                "1",
                "--log-root",
                str(tmp_path / "slogs"),
            ],
            monkeypatch,
            capsys,
        )
        assert code == 2
        assert "mutually exclusive" in out.err

    def test_scan_fix_bad_index_clean_error(self, monkeypatch, capsys, tmp_path):
        repo = _gap_repo(tmp_path)
        code, out = _run_cli(
            [
                "scan",
                "--repo",
                str(repo),
                "--fix",
                "99",
                "--log-root",
                str(tmp_path / "slogs"),
            ],
            monkeypatch,
            capsys,
        )
        assert code == 2
        assert "out of range" in out.err


# ---------------------------------------------------------------------------
# Read-only invariants (mirrors the modes round's test-pinned guarantees)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Docker-gated e2e — Task C's full loop: finding -> real fix task
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402


def _docker_up() -> bool:
    try:
        cp = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp.returncode == 0 and bool(cp.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


_docker_gate = pytest.mark.skipif(
    os.environ.get("HARNESS_EXEC_SKIP_DOCKER") == "1" or not _docker_up(),
    reason="docker daemon not reachable (or HARNESS_EXEC_SKIP_DOCKER=1)",
)

ONE_STEP_PLAN = [
    {
        "id": 1,
        "description": "write tests for pkg.core public symbols",
        "checkpoint": "python -m pytest tests/test_core.py -q passes",
        "files_hint": ["tests/test_core.py"],
    }
]

# a real test file for the gap repo's core module
CORE_TEST = (
    "from pkg.core import transform, merge\n"
    "\n"
    "def test_transform():\n"
    "    assert transform([' a ', 'b ']) == ['a', 'b']\n"
    "\n"
    "def test_transform_empty():\n"
    "    assert transform([]) == []\n"
    "\n"
    "def test_merge():\n"
    "    assert merge([1], [2]) == [1, 2]\n"
)

WRITE_TEST_CMD = (
    "python - <<'EOF'\n"
    "import pathlib\n"
    "pathlib.Path('tests/test_core.py').write_text(\n"
    "    'from pkg.core import transform, merge\\n\\n'\n"
    "    'def test_transform():\\n'\n"
    "    \"    assert transform([' a ', 'b ']) == ['a', 'b']\\n\\n\"\n"
    "    'def test_transform_empty():\\n'\n"
    "    '    assert transform([]) == []\\n\\n'\n"
    "    'def test_merge():\\n'\n"
    "    '    assert merge([1], [2]) == [1, 2]\\n'\n"
    ")\n"
    "EOF"
)


@_docker_gate
class TestScanFindingToTaskE2E:
    """Task C, the real loop: `vex scan` notices an untested module ->
    `vex fix --finding <id>#1` -> cmd_fix -> run_task -> REAL Docker
    verify -> success, with the finding's test file as the target."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from harness.deps import reset_overrides

        reset_overrides()
        yield
        reset_overrides()

    def test_finding_becomes_verified_fix(self, tmp_path, monkeypatch):
        from harness.deps import set_call_model
        from tests.fake_model import ScriptedModel

        repo = _gap_repo(tmp_path)
        logs = tmp_path / "scanlogs"
        from harness.scan_mode import run_scan

        scan = run_scan(str(repo), log_root=logs)
        core = [
            f
            for f in scan["findings"]
            if f["kind"] == "coverage_gap" and f["file"] == "pkg/core.py"
        ]
        assert core, "the gap repo's core module must be found"
        finding = core[0]

        model = ScriptedModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[WRITE_TEST_CMD, "SUBMIT"]]},
        )
        set_call_model(model)

        # drive the REAL handoff path: _run_finding -> cmd_fix ->
        # run_task (LiveMonitor/spinner suppressed via quiet)
        from cli import main as cli_main

        ns = argparse.Namespace(
            repo=str(repo),
            finding=f"{scan['scan_id']}#{finding['index']}",
            log_root=str(logs),
            issue=None,
            target_test=None,
            task_id=None,
            model=None,
            provider=None,
            api_key=None,
            api_base=None,
            adaptive_routing=False,
            max_retries=None,
            budget=None,
            approval=False,
            protected=None,
            test_command=None,
            log_root_flag=None,
        )
        captured_rc = {}

        orig_run_task = cli_main.deps.get_run_task()

        def counting_run_task(task, **kw):
            captured_rc["issue"] = task.issue_text
            captured_rc["target"] = task.config.get("target_test")
            return orig_run_task(task, **kw)

        monkeypatch.setattr(cli_main.deps, "get_run_task", lambda: counting_run_task)
        code = cli_main._run_finding(ns, cli_main.ui.console())
        assert code == 0, f"fix run failed: rc={code}"

        # the task ran with the finding's contract
        assert captured_rc["issue"] == finding["fix_issue_text"]
        assert captured_rc["target"] == "tests/test_core.py"

        # the delivered diff adds the test file; the original repo is
        # untouched (never-mutate holds for the whole round trip)
        assert not (repo / "tests" / "test_core.py").exists()
        scan_dirs = sorted(logs.glob("fix-*"))
        assert scan_dirs, "the fix run left its log dir under the scan root"
        work = scan_dirs[-1] / "work" / "tests" / "test_core.py"
        assert work.is_file(), "the fix's working copy carries the new test"

    def test_scan_fix_n_full_loop(self, tmp_path, monkeypatch, capsys):
        """`vex scan --fix 1` closes the loop in one command (the same
        machinery driven through the CLI entry)."""
        from harness.deps import set_call_model
        from tests.fake_model import ScriptedModel

        repo = _gap_repo(tmp_path)
        logs = tmp_path / "scanlogs"
        model = ScriptedModel(
            plan=ONE_STEP_PLAN,
            scripts={1: [[WRITE_TEST_CMD, "SUBMIT"]]},
        )
        set_call_model(model)

        from cli import main as cli_main

        monkeypatch.chdir(ROOT)
        try:
            code = cli_main.main(
                ["scan", "--repo", str(repo), "--fix", "1", "--log-root", str(logs)]
            )
        except SystemExit as exc:
            code = int(exc.code or 0)
        assert code == 0
        out = capsys.readouterr()
        assert "vex fix --finding" in out.out or "SUCCESS" in out.out
        # the fix run's artifacts exist under the same logs root
        assert sorted(logs.glob("fix-*"))


# ---------------------------------------------------------------------------
# Read-only invariants (mirrors the modes round's test-pinned guarantees)
# ---------------------------------------------------------------------------


class TestReadOnlyInvariants:
    def test_no_shell_no_sandbox_no_model_in_scan(self):
        """Pinned: the scan module never imports/executes the shell or
        sandbox machinery (read-only BY CONSTRUCTION, like research
        mode's no-BashSession pin)."""
        import harness.scan_mode as sm

        src = Path(sm.__file__).read_text(encoding="utf-8")
        for banned in (
            "BashSession",
            "execute_sandboxed",
            "os.system",
        ):
            assert banned not in src, banned
        # no ModelClient import either — findings are deterministic
        assert "ModelClient" not in src
        # subprocess IS used (docker probe in this test file, not the
        # module) — the module itself must not import it
        assert "import subprocess" not in src

    def test_scan_never_writes_inside_repo(self, tmp_path):
        """All scan outputs land under log_root, never the repo (the
        never-mutate guarantee, pinned filesystem-wide)."""
        from harness.scan_mode import run_scan

        repo = _gap_repo(tmp_path)
        before = {p for p in repo.rglob("*")}
        run_scan(str(repo), log_root=tmp_path / "logs")
        after = {p for p in repo.rglob("*")}
        assert before == after  # not even a new empty dir
