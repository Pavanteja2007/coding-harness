"""Read-only GitHub issue-to-task and pull-request review adapters."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1
EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3
EXIT_GITHUB = 4
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_ISSUE_FIELDS = "number,title,body,url,state,labels,assignees"


class WorkflowError(RuntimeError):
    """Base error with a stable machine-readable exit code."""

    exit_code = EXIT_ENVIRONMENT
    reason = "environment_error"


class UsageError(WorkflowError):
    """Invalid workflow input."""

    exit_code = EXIT_USAGE
    reason = "usage_error"


class GitHubError(WorkflowError):
    """GitHub CLI or network failure."""

    exit_code = EXIT_GITHUB
    reason = "github_error"


class _ArgumentParser(argparse.ArgumentParser):
    """Parser that converts usage errors into structured failures."""

    def error(self, message: str) -> None:
        raise UsageError(message)


def _validate_repo(value: str) -> str:
    """Validate one owner/repository slug."""
    repo = str(value or "").strip()
    if not _REPO_RE.fullmatch(repo):
        raise UsageError("repository must use owner/name form")
    return repo


def _validate_number(value: int) -> int:
    """Validate a positive GitHub issue or pull-request number."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise UsageError("GitHub issue or pull-request number must be positive")
    return value


def _redact(value: object, limit: int = 100_000) -> str:
    """Bound and redact common credential forms from forge content."""
    text = ("" if value is None else str(value)).replace("\x00", "")
    text = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[^\s]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"\b(?:sk|ghp|gho|ghu|ghs|ghr|xox[baprs]|hf|npm|pypi)[-_][A-Za-z0-9_-]{8,}",
        "[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bgithub_pat_[A-Za-z0-9_-]{8,}\b",
        "[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd)"
        r"\b\s*([:=])\s*([\"']?)([^\s\"',;]+)\3",
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    return text[:limit]


def _gh_environment() -> dict[str, str]:
    """Return a noninteractive GitHub CLI environment without copying Neo state."""
    env = dict(os.environ)
    env.update(
        {
            "GH_PAGER": "cat",
            "GH_PROMPT_DISABLED": "1",
            "NO_COLOR": "1",
        }
    )
    for name in tuple(env):
        if name.upper().startswith(("NEO_", "HARNESS_")):
            env.pop(name, None)
    return env


def _run_gh(arguments: list[str], timeout: int = 60) -> str:
    """Run one fixed GitHub CLI argument vector and return stdout."""
    executable = shutil.which("gh")
    if not executable:
        raise WorkflowError("GitHub CLI (gh) is unavailable")
    try:
        completed = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_gh_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError("GitHub CLI request timed out") from exc
    except OSError as exc:
        raise WorkflowError(f"GitHub CLI could not start: {exc}") from exc
    if completed.returncode != 0:
        detail = _redact(completed.stderr or completed.stdout, 1000)
        raise GitHubError(f"GitHub CLI request failed: {detail}")
    return completed.stdout


def _json_from_gh(arguments: list[str]) -> dict[str, Any]:
    """Run gh and require one JSON object."""
    try:
        payload = json.loads(_run_gh(arguments))
    except json.JSONDecodeError as exc:
        raise GitHubError(f"GitHub CLI returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GitHubError("GitHub CLI returned a non-object JSON document")
    return payload


def issue_to_task(
    repo: str,
    number: int,
    repo_path: str,
    target_test: Optional[str] = None,
) -> dict[str, Any]:
    """Convert one GitHub issue into a redacted, read-only Neo task document."""
    repo = _validate_repo(repo)
    number = _validate_number(number)
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        raise UsageError("--repo-path must not be empty")
    issue = _json_from_gh(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            _ISSUE_FIELDS,
        ]
    )
    if issue.get("number") != number:
        raise GitHubError("GitHub issue number did not match the request")
    title = _redact(issue.get("title"), 1000)
    body = _redact(issue.get("body"), 100_000)
    issue_text = f"{title}\n\n{body}".strip()
    task_id = f"github-{repo.replace('/', '-').lower()}-{number}"
    command = [
        "neo",
        "fix",
        "--repo",
        repo_path,
        "--issue",
        issue_text,
    ]
    if target_test:
        command.extend(["--target-test", str(target_test)])
    labels = sorted(
        str(item.get("name"))
        for item in issue.get("labels", [])
        if isinstance(item, dict) and item.get("name")
    )
    assignees = sorted(
        str(item.get("login"))
        for item in issue.get("assignees", [])
        if isinstance(item, dict) and item.get("login")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "github_issue_task",
        "task_id": task_id,
        "source": {
            "repository": repo,
            "issue_number": number,
            "url": _redact(issue.get("url"), 2000),
            "state": issue.get("state"),
        },
        "issue_text": issue_text,
        "labels": labels,
        "assignees": assignees,
        "command_argv": command,
        "read_only": True,
    }


def review_pull_request(repo: str, number: int, repo_path: str) -> dict[str, Any]:
    """Build a redacted, read-only PR review packet without approving or merging."""
    repo = _validate_repo(repo)
    number = _validate_number(number)
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        raise UsageError("--repo-path must not be empty")
    pull = _json_from_gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            (
                "number,title,body,url,state,isDraft,mergeable,reviewDecision,"
                "reviews,files,commits,statusCheckRollup"
            ),
        ]
    )
    if pull.get("number") != number:
        raise GitHubError("GitHub pull-request number did not match the request")
    diff = _redact(_run_gh(["pr", "diff", str(number), "--repo", repo]), 500_000)
    checks = (
        pull.get("statusCheckRollup")
        if isinstance(pull.get("statusCheckRollup"), list)
        else []
    )
    check_states = sorted(
        str(item.get("conclusion") or item.get("state") or "UNKNOWN")
        for item in checks
        if isinstance(item, dict)
    )
    checks_passed = bool(check_states) and all(
        state.upper() in {"SUCCESS", "NEUTRAL", "SKIPPED"} for state in check_states
    )
    review_decision = str(pull.get("reviewDecision") or "").upper()
    files = sorted(
        str(item.get("path"))
        for item in pull.get("files", [])
        if isinstance(item, dict) and item.get("path")
    )
    commits = pull.get("commits") if isinstance(pull.get("commits"), list) else []
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "github_pr_review",
        "source": {
            "repository": repo,
            "pull_request_number": number,
            "url": _redact(pull.get("url"), 2000),
            "state": pull.get("state"),
        },
        "repository_path": repo_path,
        "title": _redact(pull.get("title"), 1000),
        "body": _redact(pull.get("body"), 50_000),
        "draft": bool(pull.get("isDraft")),
        "mergeable": pull.get("mergeable"),
        "review_decision": review_decision or None,
        "review_count": len(
            pull.get("reviews", []) if isinstance(pull.get("reviews"), list) else []
        ),
        "checks_passed": checks_passed,
        "check_states": check_states,
        "files": files,
        "commit_count": len(commits),
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "diff": diff,
        "read_only": True,
        "mutations_performed": [],
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write one machine report atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _emit(event: str, **data: Any) -> None:
    """Write one schema-versioned event envelope."""
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "event": event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **data,
            },
            sort_keys=True,
        )
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Run a read-only GitHub workflow with stable machine output."""
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "ndjson"), default="json")
    parser.add_argument("--report", type=Path)
    subparsers = parser.add_subparsers(dest="workflow", required=True)
    issue = subparsers.add_parser("issue", help="convert an issue to a Neo task")
    issue.add_argument("--repo", required=True)
    issue.add_argument("--number", type=int, required=True)
    issue.add_argument("--repo-path", required=True)
    issue.add_argument("--target-test")
    review = subparsers.add_parser("review", help="build a read-only PR review packet")
    review.add_argument("--repo", required=True)
    review.add_argument("--number", type=int, required=True)
    review.add_argument("--repo-path", required=True)
    args: Optional[argparse.Namespace] = None
    try:
        args = parser.parse_args(argv)
        if args.format == "ndjson":
            _emit("github_workflow_started", workflow=args.workflow)
        if args.workflow == "issue":
            report = issue_to_task(
                args.repo, args.number, args.repo_path, args.target_test
            )
        else:
            report = review_pull_request(args.repo, args.number, args.repo_path)
        report.update(
            {
                "status": "pass",
                "exit_code": EXIT_SUCCESS,
                "exit_reason": "success",
            }
        )
    except WorkflowError as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "error": {
                "kind": type(exc).__name__,
                "message": _redact(exc, 2000),
                "exit_code": exc.exit_code,
                "exit_reason": exc.reason,
            },
            "exit_code": exc.exit_code,
            "exit_reason": exc.reason,
        }
    except Exception as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "status": "fail",
            "error": {
                "kind": "InternalError",
                "message": _redact(f"{type(exc).__name__}: {exc}", 2000),
                "exit_code": EXIT_ENVIRONMENT,
                "exit_reason": "internal_error",
            },
            "exit_code": EXIT_ENVIRONMENT,
            "exit_reason": "internal_error",
        }
    if args is not None and args.report is not None:
        try:
            _atomic_write(args.report.resolve(), report)
        except OSError as exc:
            print(f"workflow report write failed: {exc}", file=sys.stderr)
            return EXIT_ENVIRONMENT
    if args is not None and args.format == "ndjson":
        _emit("github_workflow_finished", report=report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
