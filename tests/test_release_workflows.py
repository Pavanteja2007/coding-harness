from __future__ import annotations

import json
import os
from email.parser import BytesParser
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from scripts import clean_room_matrix, github_workflow, verify_release

ROOT = Path(__file__).resolve().parents[1]


def test_requires_python_comparison_is_order_independent():
    metadata = BytesParser().parsebytes(
        b"Name: neo-agent-cli\nVersion: 1.0.0\nRequires-Python: <3.13,>=3.10\n"
    )
    identity = verify_release._verify_metadata(
        metadata,
        {
            "name": "neo-agent-cli",
            "version": "1.0.0",
            "requires-python": ">=3.10,<3.13",
        },
        "test metadata",
    )
    assert identity["requires_python"] == "<3.13,>=3.10"


def test_sbom_verifier_completes_root_direct_dependency_graph(tmp_path):
    project = verify_release._project(
        verify_release._load_pyproject(ROOT / "pyproject.toml")
    )
    components = []
    expected_refs = set()
    for index, value in enumerate(project["dependencies"], 1):
        requirement = Requirement(value)
        reference = f"component-{index}"
        expected_refs.add(reference)
        components.append(
            {
                "bom-ref": reference,
                "name": requirement.name,
                "type": "library",
                "version": "1.0.0",
            }
        )
    sbom = tmp_path / "neo.cdx.json"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "bom-ref": "root-component",
                        "name": project["name"],
                        "type": "application",
                        "version": project["version"],
                    }
                },
                "components": components,
                "dependencies": [],
            }
        ),
        encoding="utf-8",
    )
    result = verify_release.enrich_sbom(ROOT, sbom)
    payload = json.loads(sbom.read_text(encoding="utf-8"))
    root = next(
        item for item in payload["dependencies"] if item["ref"] == "root-component"
    )
    assert set(root["dependsOn"]) == expected_refs
    assert result["direct_dependencies"] == sorted(expected_refs)
    assert result["component_count"] == len(components)
    assert any(
        item["name"] == "neo:direct-dependency-graph-complete"
        for item in payload["metadata"]["properties"]
    )


def test_verify_release_writes_success_report_and_exact_checksums(
    tmp_path, monkeypatch, capsys
):
    artifact = tmp_path / "neo_agent_cli-1.0.0-py3-none-any.whl"
    artifact.write_bytes(b"wheel")
    sdist = tmp_path / "neo_agent_cli-1.0.0.tar.gz"
    sdist.write_bytes(b"sdist")
    report_path = tmp_path / "report.json"
    checksum_path = tmp_path / "SHA256SUMS"
    mocked = {
        "schema_version": 1,
        "status": "pass",
        "source": {"available": True, "clean": True, "tags": ["v1.0.0"]},
        "artifacts": {
            "wheel": {"path": str(artifact), "sha256": "a" * 64},
            "sdist": {"path": str(sdist), "sha256": "b" * 64},
        },
    }
    monkeypatch.setattr(verify_release, "verify_dist", lambda *args, **kwargs: mocked)
    result = verify_release.main(
        [
            "--project-root",
            str(ROOT),
            "--dist",
            str(tmp_path),
            "--report",
            str(report_path),
            "--checksums",
            str(checksum_path),
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "pass"
    assert checksum_path.read_text(encoding="utf-8") == (
        f"{'a' * 64}  {artifact.name}\n{'b' * 64}  {sdist.name}\n"
    )


def test_verify_release_failure_is_machine_readable_and_keeps_evidence(
    tmp_path, monkeypatch, capsys
):
    report_path = tmp_path / "failure.json"
    mocked = {
        "schema_version": 1,
        "source": {"available": True, "clean": False, "dirty_paths": ["candidate.py"]},
        "artifacts": {
            "wheel": {"path": "wheel.whl", "sha256": "a" * 64},
            "sdist": {"path": "sdist.tar.gz", "sha256": "b" * 64},
        },
    }
    monkeypatch.setattr(verify_release, "verify_dist", lambda *args, **kwargs: mocked)
    result = verify_release.main(
        [
            "--project-root",
            str(ROOT),
            "--dist",
            str(tmp_path),
            "--require-clean",
            "--report",
            str(report_path),
        ]
    )
    assert result == verify_release.EXIT_VERIFICATION
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "fail"
    assert payload["error"]["exit_reason"] == "verification_failed"
    assert payload["artifacts"]["wheel"]["sha256"] == "a" * 64
    assert json.loads(report_path.read_text(encoding="utf-8"))["error"]


def test_verify_release_ndjson_is_one_event_per_line(tmp_path, monkeypatch, capsys):
    mocked = {
        "schema_version": 1,
        "source": {"available": True, "clean": True, "tags": []},
        "artifacts": {
            "wheel": {"path": "wheel.whl", "sha256": "a" * 64},
            "sdist": {"path": "sdist.tar.gz", "sha256": "b" * 64},
        },
    }
    monkeypatch.setattr(verify_release, "verify_dist", lambda *args, **kwargs: mocked)
    assert (
        verify_release.main(
            ["--project-root", str(ROOT), "--dist", str(tmp_path), "--events"]
        )
        == 0
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["event"] for event in events] == [
        "release_verification_started",
        "artifact_verified",
        "artifact_verified",
        "release_verification_finished",
    ]
    assert all(event["schema_version"] == 1 for event in events)


def test_github_issue_to_task_is_fixed_argv_and_redacted(monkeypatch):
    calls = []

    def fake_run(arguments, timeout=60):
        calls.append(arguments)
        return json.dumps(
            {
                "number": 42,
                "title": "Crash with api_key=sk-live-1234567890abcdef",
                "body": "Steps to reproduce",
                "url": "https://github.com/acme/tool/issues/42",
                "state": "OPEN",
                "labels": [{"name": "bug"}, {"name": "release"}],
                "assignees": [{"login": "octocat"}],
            }
        )

    monkeypatch.setattr(github_workflow, "_run_gh", fake_run)
    task = github_workflow.issue_to_task("acme/tool", 42, "C:/repo", "tests/test_x.py")
    assert task["task_id"] == "github-acme-tool-42"
    assert task["read_only"] is True
    assert "sk-live" not in json.dumps(task)
    assert task["command_argv"][:4] == ["neo", "fix", "--repo", "C:/repo"]
    assert task["command_argv"][-2:] == ["--target-test", "tests/test_x.py"]
    assert calls == [
        [
            "issue",
            "view",
            "42",
            "--repo",
            "acme/tool",
            "--json",
            github_workflow._ISSUE_FIELDS,
        ]
    ]


def test_github_pr_review_is_read_only_and_hash_bound(monkeypatch):
    def fake_run(arguments, timeout=60):
        if arguments[:2] == ["pr", "view"]:
            return json.dumps(
                {
                    "number": 7,
                    "title": "Release candidate",
                    "body": "Authorization: Bearer ghp_1234567890abcdef",
                    "url": "https://github.com/acme/tool/pull/7",
                    "state": "OPEN",
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                    "reviewDecision": "APPROVED",
                    "reviews": [{"state": "APPROVED"}],
                    "files": [{"path": "b.py"}, {"path": "a.py"}],
                    "commits": [{"oid": "abc"}],
                    "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                }
            )
        return "diff --git a/a.py b/a.py\n+token=github_pat_1234567890abcdefghijk"

    monkeypatch.setattr(github_workflow, "_run_gh", fake_run)
    review = github_workflow.review_pull_request("acme/tool", 7, "/repo")
    assert review["files"] == ["a.py", "b.py"]
    assert review["checks_passed"] is True
    assert review["mutations_performed"] == []
    assert "ghp_" not in json.dumps(review)
    assert "github_pat_" not in json.dumps(review)
    assert len(review["diff_sha256"]) == 64


@pytest.mark.parametrize("repo", ("", "owner", "../owner/repo", "owner/repo;echo"))
def test_github_workflow_rejects_unsafe_repository(repo, capsys):
    result = github_workflow.main(
        ["issue", "--repo", repo, "--number", "1", "--repo-path", "."]
    )
    assert result == github_workflow.EXIT_USAGE
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["exit_reason"] == "usage_error"


def test_clean_room_environment_is_hermetic_and_path_first(tmp_path):
    env = clean_room_matrix._base_environment(
        tmp_path / "output",
        tmp_path / "lane",
        tmp_path / "work",
        tmp_path / "venv",
    )
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert env["PIP_NO_CACHE_DIR"] == "1"
    assert "PIP_CACHE_DIR" not in env
    assert (
        env["PATH"]
        .split(os.pathsep)[0]
        .endswith("Scripts" if os.name == "nt" else "bin")
    )
    assert {
        "path_resolution",
        "sdk_smoke",
        "login",
        "logout",
        "git_output",
        "update_check",
        "uninstall",
        "uninstall_complete",
    } <= set(clean_room_matrix._CHECK_NAMES)


@pytest.mark.parametrize("task_end_status", ("success", "completed_verified"))
def test_clean_room_accepts_verified_terminal_status(monkeypatch, task_end_status):
    events = [
        {
            "kind": "baseline_verify",
            "data": {"target_passed_on_pristine": False},
        },
        {
            "kind": "final_verify",
            "data": {
                "target_passed": True,
                "regression_passed": True,
                "flaky": False,
            },
        },
        {"kind": "task_end", "data": {"status": task_end_status}},
        {"kind": "result", "data": {"status": "success"}},
    ]
    monkeypatch.setattr(clean_room_matrix, "_trace_events", lambda log_root: events)
    evidence = clean_room_matrix._verify_fix_trace(ROOT / "logs" / "unused")
    assert evidence["passed"] is True
    assert evidence["evidence"]["task_end_status"] == task_end_status


def test_release_metadata_and_lock_are_reproducible():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10,<3.13"' in text
    assert '"build==1.6.1"' in text
    assert '"cyclonedx-bom==7.4.0"' in text
    assert '"twine==7.0.0"' in text
    assert '"uv==0.11.14"' in text
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10, <3.13"' in lock
    assert 'name = "neo-agent-cli"' in lock
    assert 'hash = "sha256:' in lock


def test_release_inventory_includes_product_packages_and_recipe_data():
    data = verify_release._load_pyproject(ROOT / "pyproject.toml")
    packages = set(data["tool"]["setuptools"]["packages"])
    assert {
        "acp",
        "agent_sdk",
        "extensions",
        "integrations",
        "recipes",
        "recipes.builtin",
    } <= packages
    modules, data_files = verify_release._source_payload(data, ROOT)
    assert {
        "agent_sdk/__init__.py",
        "agent_sdk/client.py",
        "agent_sdk/local.py",
        "agent_sdk/remote.py",
        "agent_sdk/server.py",
    } <= modules
    assert "recipes/__init__.py" in modules
    assert "recipes/builtin/__init__.py" in modules
    assert "recipes/builtin/portable_code_review.yaml" in data_files
    assert "recipes/builtin/summarize_findings.yaml" in data_files


def test_release_gate_has_required_lanes_and_exact_artifact_upload():
    text = (ROOT / ".github" / "workflows" / "release-gate.yml").read_text(
        encoding="utf-8"
    )
    for value in (
        "3.10",
        "3.11",
        "3.12",
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
    ):
        assert value in text
    for lane in (
        "python -m pytest --collect-only",
        "test_cli_release.py",
        "test_installers_release.py",
        "test_cli_selfupdate_release.py",
        "test_cli_uninstall_release.py",
        "test_installed_user_flow.py",
        "test_daily_driver_evals.py",
        "test_evals_run.py",
        "test_tui_contract.py",
        "python -m build",
        "python -m twine check",
        "clean_room_matrix.py",
        "cyclonedx-py",
        "JsonStrictValidator",
        "SHA256SUMS",
        "install.sh",
        "install.ps1",
        "install.cmd",
    ):
        assert lane in text
    assert "uv sync --locked" in text
    assert "uv run --locked --all-extras" in text
    assert "--ignore-path release-dist-a" in text
    assert "--ignore-path release-dist-b" in text
    assert "--sbom release-dist/neo-agent-cli.cdx.json" in text
    assert "twine upload" not in text
    assert "release-dist/*" not in text
    assert "dist/*" not in text
