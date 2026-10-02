"""`/doctor` — read-only daily-path health checks and recovery actions.

Checks: Docker, git identity, provider reachability, litellm, Textual,
.neo settings writability, worktree root, MCP servers, and the snapshot
disk budget/retention state.  Nothing here mutates the repository, the
settings, or the logs; a failed check names the command that would
remediate it.

CLI surfaces
------------
- `/doctor`                human-readable, one actionable summary line
- `/doctor --json`         machine-readable; every check with status,
                           reason, evidence, and remediation

The check set is one place to grow; the JSON renderer and the human
renderer both read the same records, so `doctor --json` and the TUI /
REPL views never drift apart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.markup import escape

from cli import neoconfig
from execution.sandbox import docker_available
from memory.paths import default_logs_dir, snapshot_disk_receipt

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "DOCTOR_CHECKS",
    "DoctorCheck",
    "SupportBundle",
    "build_support_bundle",
    "check_connector_permissions",
    "check_docker",
    "check_git_identity",
    "check_litellm",
    "check_mcp_servers",
    "check_neo_writable",
    "check_provider_reachability",
    "check_snapshot_disk",
    "check_textual",
    "check_worktree_root",
    "render_doctor_human",
    "render_doctor_json",
    "render_support_bundle_manifest",
    "run_doctor",
    "write_support_bundle",
]

_DOCTOR = "doctor"

#: The support-bundle document version. A bundle carrying any other value is
#: refused by a reader rather than half-understood, exactly like every other
#: versioned artifact in this project.
BUNDLE_SCHEMA_VERSION = 1

#: Environment variables whose NAMES are recorded (never their values). A name
#: tells a maintainer which provider the operator is pointing at, which is most
#: of what a support request needs, and cannot leak a credential.
_BUNDLE_ENV_NAMES: Tuple[str, ...] = (
    "NEO_HOME",
    "NEO_CONFIG",
    "NEO_GLOBAL_ROOT",
    "NEO_PROJECT_DIR",
    "NEO_MODEL",
    "NEO_PROVIDER",
    "NEO_API_BASE",
    "NEO_BASE_URL",
    "NEO_LOGS_DIR",
    "NEO_PLUGINS_DIR",
    "NEO_HOOKS_DIR",
    "NEO_EGRESS_ALLOWED_HOSTS",
    "NEO_NOTIFY",
    "HARNESS_HOME",
    "HARNESS_LOGS_DIR",
    "HARNESS_EXEC_SKIP_DOCKER",
    "NEO_TRACE_DIR",
)

#: Journal event names that count as "an error worth reading", and how many of
#: each a bundle carries. A bundle is a diagnostic, not an archive: it takes the
#: most recent few of each so the bundle stays small and readable.
_BUNDLE_ERROR_EVENTS: Tuple[str, ...] = (
    "error",
    "task_error",
    "model_failure_terminal",
    "verification_failed",
    "test_config_guard_failed",
    "steering_abort_watcher_error",
    "knowledge_unavailable",
    "memory_record_refused",
    "subagent_spawn_refused",
    "merge_failed",
)
_DOCTER = "doctor"
_BUNDLE_ERROR_LIMIT_PER_EVENT = 5

_BUNDLE_MAX_RUNS_SCANNED = 200


@dataclass(frozen=True)
class DoctorCheck:
    """One read-only health check.

    ``kind`` is the json-schema slug; ``probe`` holds the callable that
    produced the result.  ``probe`` is invoked lazily by the manager so a
    failed early check cannot stop later checks from running.
    """

    name: str
    kind: str
    label: str
    probe: Callable[[], Dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: The log root the current `run_doctor` call was asked about.  `DoctorCheck.probe`
#: takes no arguments (the registry contract several tests pin), so the disk
#: check reads this instead of re-deriving a root and disagreeing with the
#: record the caller asked for.  Empty means "use the default root".
_ACTIVE_LOG_ROOT: Optional[Path] = None


def _human_bytes(value: Optional[int]) -> str:
    """Render a byte count for a one-line evidence string."""
    if value is None:
        return "unknown"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024.0 or unit == "TiB":
            return f"{int(number)} B" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024.0
    return f"{int(number)} B"  # pragma: no cover - loop always returns


def _run(*cmd: str, timeout: float = 10.0) -> Tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr) — never raises."""
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _python() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_docker() -> Dict[str, Any]:
    """Docker is the execution boundary; a missing daemon cannot be repaired
    by this process."""
    available = docker_available()
    if available is None:
        return {
            "status": "error",
            "reason": "could not determine Docker availability",
            "evidence": "docker binary absent or permission denied",
            "remediation": "start the Docker daemon or run with "
            "NEO_EXEC_SKIP_DOCKER=1 for an environment without it",
        }
    if available:
        return {
            "status": "ok",
            "reason": "Docker is running",
            "evidence": "docker info succeeded",
            "remediation": None,
        }
    return {
        "status": "failed",
        "reason": "Docker daemon is unreachable",
        "evidence": "docker_available() returned False",
        "remediation": "start Docker (system tray / services.msc), then re-run",
    }


def check_git_identity() -> Dict[str, Any]:
    """A fix run needs a commit identity even when it does not push."""
    rc, out, _err = _run("git", "var", "GIT_COMMITTER_IDENT")
    if rc == 0 and out.strip():
        email = out.strip().split("<")[-1].split(">")[0]
        return {
            "status": "ok",
            "reason": "commit identity is configured",
            "evidence": email,
            "remediation": None,
        }
    rc2, out2, _ = _run("git", "config", "user.name")
    rc3, out3, _ = _run("git", "config", "user.email")
    if rc2 == 0 and rc3 == 0 and out2.strip() and out3.strip():
        return {
            "status": "deprecated",
            "reason": "local identity set; global not configured",
            "evidence": f"{out2.strip()} <{out3.strip()}>",
            "remediation": "git config --global user.name / user.email",
        }
    return {
        "status": "failed",
        "reason": "no git identity configured",
        "evidence": "GIT_COMMITTER_IDENT empty",
        "remediation": 'git config --global user.name "<name>"; '
        'git config --global user.email "<email>"',
    }


def check_provider_reachability() -> Dict[str, Any]:
    """Provider/model reachability.

    Uses the same bounded credential probe ``neo login`` uses, so a
    green check here means the same thing a green wizard does.  With no
    model configured the result is ``ok`` with a note rather than a
    failure: an unconfigured provider is onboarding state, not breakage.
    """
    try:
        from cli import neoconfig, onboard

        effective = neoconfig.merged_settings()
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"could not read the effective settings: {type(exc).__name__}",
            "evidence": "",
            "remediation": "neo config list",
        }
    model = str(effective.get("model") or "").strip()
    if not model:
        return {
            "status": "ok",
            "reason": "no model configured yet (onboarding runs on first use)",
            "evidence": "run `neo login` to configure a provider",
            "remediation": None,
        }
    provider = str(effective.get("provider") or "").strip() or None
    api_base = effective.get("api_base") or effective.get("base_url") or None
    api_key = str(effective.get("api_key") or "")
    try:
        result = onboard.health_check(
            provider,
            model,
            api_key,
            str(api_base) if api_base else None,
            timeout_s=20,
        )
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"provider probe raised: {type(exc).__name__}",
            "evidence": "",
            "remediation": "neo login",
        }
    if result.get("ok"):
        return {
            "status": "ok",
            "reason": f"{model} answered a live credential probe",
            "evidence": str(api_base or provider or "default endpoint"),
            "remediation": None,
        }
    return {
        "status": "failed",
        "reason": f"provider probe failed for {model}",
        "evidence": str(result.get("error") or "no detail"),
        "remediation": "neo login (reconfigure), or check the endpoint's status",
    }


def check_litellm() -> Dict[str, Any]:
    """litellm is the model-access layer: the daily path needs it when a
    real provider is configured."""
    if not _importable("litellm"):
        return {
            "status": "failed",
            "reason": "litellm is not installed",
            "evidence": "import failed",
            "remediation": "pip install litellm",
        }
    try:
        import litellm

        version = getattr(litellm, "__version__", "unknown")
        return {
            "status": "ok",
            "reason": "litellm importable",
            "evidence": f"litellm {version}",
            "remediation": None,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason": "litellm present but unusable",
            "evidence": f"{type(exc).__name__}: {exc}",
            "remediation": "reinstall litellm (pip install --force-reinstall litellm)",
        }


def check_textual() -> Dict[str, Any]:
    """Textual powers the full-screen TUI; a missing import means the TUI
    surfaces fall back to the REPL without a warning."""
    if _importable("textual"):
        try:
            import textual

            version = getattr(textual, "__version__", "unknown")
            return {
                "status": "ok",
                "reason": "textual importable",
                "evidence": f"textual {version}",
                "remediation": None,
            }
        except Exception as exc:
            return {
                "status": "failed",
                "reason": "textual importable but unusable",
                "evidence": f"{type(exc).__name__}: {exc}",
                "remediation": "pip install textual>=0.40",
            }
    return {
        "status": "skipped",
        "reason": "textual not installed (fallback to REPL is in effect)",
        "evidence": "import failed",
        "remediation": None,
    }


def check_neo_writable() -> Dict[str, Any]:
    """.neo settings writability gates the config command surface; a read-
    only home cannot save model/provider/log-root changes."""
    candidates = [
        neoconfig.global_settings_path(),
        neoconfig.legacy_settings_path(),
        neoconfig.project_settings_path(start=Path.cwd()),
    ]
    for path in candidates:
        if path is None:
            continue
        try:
            parent = path.parent
            if not parent.exists():
                continue
            test = parent / (path.name + ".doctor_write_test")
            test.write_text("", encoding="utf-8")
            test.unlink(missing_ok=True)
            return {
                "status": "ok",
                "reason": ".neo settings writable",
                "evidence": str(path),
                "remediation": None,
            }
        except Exception as exc:
            return {
                "status": "failed",
                "reason": ".neo settings not writable",
                "evidence": f"{path}: {type(exc).__name__}",
                "remediation": "make the settings directory writable, "
                "or run with a writable HARNESS_HOME",
            }
    return {
        "status": "failed",
        "reason": "no .neo settings file found and none writable",
        "evidence": "settings chain empty or unwritable",
        "remediation": "run `neo` once to scaffold the settings, "
        "or set HARNESS_HOME to a writable directory",
    }


def check_worktree_root() -> Dict[str, Any]:
    """A detached/orphaned worktree root confuses repo switching; the
    daily path keeps session state keyed by the repo the user is in."""
    root = Path.cwd().resolve()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=5.0,
            text=True,
            cwd=str(root),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            toplevel = Path(proc.stdout.strip())
            if toplevel.exists():
                return {
                    "status": "ok",
                    "reason": "worktree root resolved",
                    "evidence": str(toplevel),
                    "remediation": None,
                }
    except Exception:
        pass
    return {
        "status": "failed",
        "reason": "cannot resolve worktree root (not a git repo)",
        "evidence": str(root),
        "remediation": "run `neo` inside a git checkout, or set "
        "NEO_PROJECT_DIR to the .neo directory",
    }


def check_mcp_servers() -> Dict[str, Any]:
    """MCP servers listed in settings are consumed by the daily loop; a
    broken server is reported so the user can repair it."""
    try:
        from cli import connectors

        servers = connectors.discover_mcp_servers()
    except Exception:
        return {
            "status": "skipped",
            "reason": "connector registry unavailable",
            "evidence": "discover_mcp_servers raised",
            "remediation": None,
        }
    # `discover_mcp_servers()` returns {label: {"command": ..., "source": ...}},
    # NOT a sequence of (label, command) pairs. Iterating it as pairs unpacks
    # the dict KEYS, so a label longer than two characters raised
    # "ValueError: too many values to unpack (expected 2)" -- which made
    # `neo doctor` fail on a FRESH install with no MCP servers configured at
    # all, i.e. on the very first command a new user runs. Found by installing
    # the built 0.3.0 wheel into a clean venv and running the real journey.
    names = list(servers)
    if not names:
        return {
            "status": "ok",
            "reason": "no MCP servers configured",
            "evidence": "nothing to probe",
            "remediation": "neo mcp add <label> -- <cmd...>  (optional)",
        }
    broken: List[str] = []
    for label in names:
        try:
            ok = connectors.check_health(label, timeout_s=3.0)
        except Exception:
            ok = False
        if not ok:
            broken.append(label)
    if not broken:
        return {
            "status": "ok",
            "reason": f"{len(names)} MCP server(s) healthy",
            "evidence": ", ".join(names),
            "remediation": None,
        }
    return {
        "status": "failed",
        "reason": f"{len(broken)} of {len(names)} MCP server(s) unhealthy",
        "evidence": ", ".join(broken) or ", ".join(names),
        "remediation": "neo mcp health <label>; fix or remove the server",
    }


def check_snapshot_disk() -> Dict[str, Any]:
    """Run directories, the shared content store, free space, and the policy.

    A run directory holds a ``pristine`` reference copy AND a ``work`` copy of
    the target source tree, which measured ~1.2 GB per run before ignored
    directories were honoured. This row makes that cost visible and the policy
    that bounds it inspectable, because the failure mode it exists to prevent
    (filling the user's disk) is invisible until it happens.

    Status vocabulary here is deliberate: ``failed`` only when something is
    actually wrong NOW -- free space below the reserve, or the measured tree
    over the operator's ceiling. A large tree that is within policy is
    ``ok`` with the numbers in its evidence, because "you have 163 MB of run
    directories" is information, not breakage. An unmeasurable root is
    ``error`` with the reason, never a fabricated zero.
    """
    root = _ACTIVE_LOG_ROOT
    try:
        receipt = snapshot_disk_receipt(log_root=root)
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"disk receipt failed: {type(exc).__name__}",
            "evidence": "",
            "remediation": "python -m execution.snapshot --json",
        }
    log_root = str(receipt.get("log_root") or "")
    free = receipt.get("free_bytes")
    total = receipt.get("total_bytes")
    runs = receipt.get("run_directories")
    store = receipt.get("store") or {}
    budget = receipt.get("budget") or {}
    policy = receipt.get("retention") or {}
    problem = str(receipt.get("error") or "")

    evidence_parts: List[str] = [
        f"log root {log_root}",
        f"{_human_bytes(total)} in {runs if runs is not None else '?'} run dir(s)",
        f"free {_human_bytes(free)}",
    ]
    if receipt.get("pristine_bytes") is not None:
        evidence_parts.append(
            f"pristine {_human_bytes(receipt['pristine_bytes'])} / "
            f"work {_human_bytes(receipt.get('work_bytes'))}"
        )
    if store.get("exists"):
        evidence_parts.append(
            f"shared store {store.get('blobs', 0)} blob(s) "
            f"{_human_bytes(store.get('bytes'))}"
        )
    if policy:
        evidence_parts.append(
            "retention keep_latest="
            f"{policy.get('keep_latest')} max_age_days="
            f"{policy.get('max_age_days')} store_max="
            f"{_human_bytes(policy.get('store_max_bytes'))}"
        )
    if budget:
        ceiling = budget.get("budget_bytes")
        evidence_parts.append(
            "budget ceiling="
            + ("none declared" if ceiling is None else _human_bytes(ceiling))
            + f" reserve={_human_bytes(budget.get('reserve_bytes'))}"
            + f" ({budget.get('reason')})"
        )
    evidence = "; ".join(evidence_parts)
    prune_command = (
        f"python -m execution.snapshot --log-root {log_root or '<log-root>'} --dry-run"
    )

    if problem:
        return {
            "status": "error",
            "reason": problem,
            "evidence": evidence,
            "remediation": f"{prune_command} to inspect; nothing was deleted",
        }
    if not receipt.get("log_root_exists"):
        return {
            "status": "ok",
            "reason": "no run directory has been created yet",
            "evidence": f"log root {log_root} does not exist",
            "remediation": None,
        }
    if budget.get("allowed") is False:
        return {
            "status": "failed",
            "reason": (
                f"run artifact disk use is outside its budget ({budget.get('reason')})"
            ),
            "evidence": evidence,
            "remediation": (
                f"{prune_command}; then apply with --prune, or raise "
                "snapshot_budget_bytes / snapshot_reserve_bytes for the task"
            ),
        }
    return {
        "status": "ok",
        "reason": "run artifact disk use is within its budget",
        "evidence": evidence,
        "remediation": None,
    }


def check_spend_receipts() -> Dict[str, Any]:
    """Every model call this machine spent money on has a receipt.

    R2-17 (item 4). The conversation's own `trace.jsonl` usage rows only
    cover the MAIN conversation. The router's per-call ledger
    (`{task_id}.runtime/model_ledger.jsonl`, Boundary 2) records EVERY
    attempt — the routing classifier, a retry, a failed provider call, a
    call made outside the conversation. Before this check existed, that
    spend had no audit trail at all, so `/cost` under-reported it and no
    surface could notice.

    Reports the worst shortfall across recent runs: how many runs have no
    ledger at all, and how many carry UNPRICED calls. An unpriced call
    counts as a shortfall rather than as free, because "we did not
    measure it" and "it cost nothing" are different claims. Read-only and
    bounded by `_BUNDLE_MAX_RUNS_SCANNED`; never raises.
    """
    root = Path(_ACTIVE_LOG_ROOT) if _ACTIVE_LOG_ROOT else None
    if root is None or not root.is_dir():
        return {
            "status": "ok",
            "reason": "no run directory to audit yet",
            "evidence": "no logs root resolved for this check",
            "remediation": None,
        }
    from cli.runview import model_call_receipts

    runs = 0
    unledgered: List[str] = []
    unpriced = 0
    unpriced_runs: List[str] = []
    try:
        for task_id, run_dir in _iter_run_dirs(root):
            if runs >= _BUNDLE_MAX_RUNS_SCANNED:
                break
            if not (Path(run_dir) / "trace.jsonl").is_file():
                continue
            runs += 1
            ledger = Path(run_dir).parent / f"{task_id}.runtime" / "model_ledger.jsonl"
            if not ledger.is_file():
                unledgered.append(str(task_id))
                continue
            try:
                receipts = model_call_receipts(root, str(task_id))
            except Exception:
                unledgered.append(str(task_id))
                continue
            missing = sum(1 for row in receipts if not row.get("priced"))
            if missing:
                unpriced += missing
                unpriced_runs.append(f"{task_id} ({missing} unpriced)")
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"could not audit the model-call ledgers: {type(exc).__name__}",
            "evidence": str(exc)[:160],
            "remediation": "check the logs root is readable",
        }
    if not runs:
        return {
            "status": "ok",
            "reason": "no completed runs to audit yet",
            "evidence": f"{root} holds no run with a trace.jsonl",
            "remediation": None,
        }
    if not unledgered and not unpriced:
        return {
            "status": "ok",
            "reason": f"every model call in {runs} run(s) has a priced receipt",
            "evidence": (
                f"{runs} run(s) audited under {root}; "
                f"{_BUNDLE_MAX_RUNS_SCANNED - runs} more would be scanned"
                if runs >= _BUNDLE_MAX_RUNS_SCANNED
                else f"{runs} run(s) audited under {root}"
            ),
            "remediation": None,
        }
    reasons = []
    if unledgered:
        reasons.append(
            f"{len(unledgered)} run(s) have no model ledger "
            f"({', '.join(unledgered[:3])})"
        )
    if unpriced:
        reasons.append(
            f"{unpriced} call(s) in {len(unpriced_runs)} run(s) were never priced"
        )
    return {
        "status": "failed",
        "reason": "model spend without a complete audit trail: " + "; ".join(reasons),
        "evidence": f"{runs} run(s) audited under {root}",
        "remediation": (
            "runs without a ledger predate the per-call ledger; "
            "/cost reports their conversation-only spend and says so. "
            "Set a model price (settings `model_prices`) so a call is "
            "priced instead of counted as unknown."
        ),
    }


def _iter_run_dirs(root: Path) -> List[Tuple[str, Path]]:
    """(task_id, run_dir) for every run directory under `root`, bounded.

    Skips index/hidden directories and `.runtime` sidecars, which is the
    same exclusion the session/cost surfaces use. Never raises.
    """
    out: List[Tuple[str, Path]] = []
    try:
        for entry in sorted(root.iterdir()):
            name = entry.name
            if not entry.is_dir():
                continue
            if name.startswith((".", "_")) or name.endswith(".runtime"):
                continue
            out.append((name, entry))
            if len(out) >= _BUNDLE_MAX_RUNS_SCANNED:
                break
    except OSError:
        return out
    return out


def check_connector_permissions(repo_path: Optional[str] = None) -> Dict[str, Any]:
    """Every configured connector states its blast radius.

    This check exists because "this connector can do anything its server
    offers" used to be invisible: the only record was a launch command, and a
    launch command says nothing about what the server will be asked to do. A
    connector with no declaration is reported as ACTIONABLE with the exact
    remediation, because the fix is one command and the absence is the whole
    point of the check.

    A connector that IS declared is reported with its declaration's summary so
    a reviewer reading the health output can see the ceiling without opening
    the TOML file. A permissions file that exists but cannot be parsed is an
    ``error``, not a pass — "we could not read the declarations" must never
    render as "the declarations are fine".
    """
    try:
        from cli.connectors import (
            ConnectorError,
            discover_mcp_servers,
            read_permissions,
        )
    except Exception as exc:  # pragma: no cover - import-time breakage
        return {
            "status": "error",
            "reason": f"cli.connectors is unavailable: {exc}",
            "evidence": "",
            "remediation": None,
        }
    try:
        servers = discover_mcp_servers(repo_path)
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"connector discovery failed: {type(exc).__name__}: {exc}",
            "evidence": "",
            "remediation": None,
        }
    try:
        declared = read_permissions(repo_path)
    except ConnectorError as exc:
        return {
            "status": "error",
            "reason": f"a connector permission declaration is unreadable: {exc}",
            "evidence": "",
            "remediation": (
                "fix the declaration file by hand; it was NOT interpreted, so no "
                "connector is currently considered declared"
            ),
        }
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"could not read connector permissions: {type(exc).__name__}: {exc}",
            "evidence": "",
            "remediation": None,
        }
    undeclared = sorted(label for label in servers if label not in declared)
    summary = {
        "connectors": len(servers),
        "declared": len(servers) - len(undeclared),
        "undeclared": undeclared,
        "declarations": {
            label: declared[label].to_dict() for label in sorted(declared)
        },
    }
    if undeclared:
        return {
            "status": "failed",
            "reason": (
                f"{len(undeclared)} of {len(servers)} connector(s) have no declared "
                f"permissions: {undeclared}"
            ),
            "evidence": ", ".join(
                f"{label} ({servers[label].get('source', '?')})" for label in undeclared
            ),
            "remediation": (
                "declare each one: neo mcp permissions <label> --tool <name> "
                "[--side-effect read|search|network|mutation|destructive] "
                "[--network <host>] [--no-write|--write]"
            ),
        }
    unstated_write = sorted(
        label for label, permission in declared.items() if not permission.write_declared
    )
    if unstated_write:
        return {
            "status": "ok",
            "reason": (
                f"all {len(servers)} connector(s) declare permissions, but "
                f"{len(unstated_write)} have not stated their file-write capability"
            ),
            "evidence": ", ".join(unstated_write),
            "remediation": (
                "state it: neo mcp permissions <label> --no-write (or --write)"
            ),
            "summary": summary,
        }
    return {
        "status": "ok",
        "reason": f"all {len(servers)} connector(s) declare their permissions",
        "evidence": ", ".join(
            f"{label}: tools={declared[label].tools or '[]'} "
            f"side_effect<={declared[label].side_effect} "
            f"write={declared[label].write}"
            for label in sorted(declared)
        )
        or "no connectors configured",
        "remediation": None,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Registry + runner
# ---------------------------------------------------------------------------

_ACTIVE_LOG_ROOT = None

_DOCTOR_CHECKS: Tuple[DoctorCheck, ...] = (
    DoctorCheck("docker", "docker", "Docker", check_docker),
    DoctorCheck("git_identity", "git", "Git identity", check_git_identity),
    DoctorCheck(
        "provider_reachability",
        "provider",
        "Provider reachability",
        check_provider_reachability,
    ),
    DoctorCheck("litellm", "litellm", "litellm", check_litellm),
    DoctorCheck("textual", "textual", "Textual", check_textual),
    DoctorCheck("neo_writable", "settings", ".neo writable", check_neo_writable),
    DoctorCheck("worktree_root", "repo", "Worktree root", check_worktree_root),
    DoctorCheck("mcp_servers", "mcp", "MCP servers", check_mcp_servers),
    DoctorCheck(
        "connector_permissions",
        "mcp",
        "Connector permissions",
        check_connector_permissions,
    ),
    DoctorCheck("snapshot_disk", "disk", "Snapshot disk", check_snapshot_disk),
    DoctorCheck("spend_receipts", "cost", "Model spend receipts", check_spend_receipts),
)


#: Public alias so callers can enumerate the checks without re-deriving them.
DOCTOR_CHECKS = _DOCTOR_CHECKS


def run_doctor(
    repo_path: Optional[str] = None, log_root: Optional[str] = None
) -> Dict[str, Any]:
    """Run every doctor check and return the machine-readable record."""
    global _ACTIVE_LOG_ROOT
    started = _now()
    _ACTIVE_LOG_ROOT = Path(log_root) if log_root else None
    try:
        return _collect(repo_path, log_root, started)
    finally:
        # The probe contract is zero-argument, so the active root is module
        # state. Clearing it keeps a later direct `check_snapshot_disk()` call
        # from reporting a root that belonged to an earlier, unrelated call.
        _ACTIVE_LOG_ROOT = None


def _collect(
    repo_path: Optional[str], log_root: Optional[str], started: str
) -> Dict[str, Any]:
    """Run every registered check and assemble the record.

    Split out of :func:`run_doctor` so the module-level active log root has a
    scope that a ``finally`` can clear; a probe that raises is still recorded
    as an ``error`` row rather than escaping.
    """
    results: List[Dict[str, Any]] = []
    for check in _DOCTOR_CHECKS:
        try:
            results.append(
                {
                    "key": check.name,
                    "kind": check.kind,
                    "label": check.label,
                    "status": "ok",
                    "reason": "",
                    "evidence": "",
                    "remediation": None,
                }
            )
            outcome = check.probe()
            results[-1]["status"] = outcome["status"]
            results[-1]["reason"] = outcome["reason"]
            results[-1]["evidence"] = outcome["evidence"]
            results[-1]["remediation"] = outcome["remediation"]
        except Exception as exc:
            results[-1]["status"] = "error"
            results[-1]["reason"] = f"check raised: {type(exc).__name__}: {exc}"
            results[-1]["evidence"] = ""
            results[-1]["remediation"] = None
    actionable = [
        row for row in results if row["status"] in {"failed", "error", "skipped"}
    ]
    record = {
        "schema_version": 1,
        "command": _DOCTER,
        "task_id": "",
        "repo_path": repo_path or str(Path.cwd()),
        "log_root": log_root or str(default_logs_dir()),
        "run_at": started,
        "checks": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["status"] == "ok"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "errored": sum(1 for r in results if r["status"] == "error"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "actionable": len(actionable),
        },
        "actionable_failures": actionable,
    }
    return record


# ---------------------------------------------------------------------------
# Renderers (shared by human + JSON surfaces)
# ---------------------------------------------------------------------------


def render_doctor_json(record: Dict[str, Any]) -> str:
    """Return the machine-readable document."""
    return json.dumps(record, indent=2, sort_keys=True)


def render_doctor_human(record: Dict[str, Any]) -> str:
    """Return a short, actionable summary for the terminal.

    Every machine-sourced field is escaped. `label`, `reason`, `evidence` and
    `remediation` all carry data Neo did not author -- a Docker error string, a
    path, a config key, an owner email -- and any `[` in them reached the markup
    parser live. Measured consequences: evidence containing `Ann [core]
    <a[b]@x.io>` rendered with the bracketed text DELETED, and evidence ending
    in a stray `[/]` raised MarkupError which, because the whole report is one
    string, lost every line of the report. Escaping matches
    `interactive.render_help` and `runview.status_lines`, which already do.
    """
    dash = "\u2014"
    lines: List[str] = []
    summary = record.get("summary") or {}
    lines.append(
        f"[neo.muted]doctor: {summary.get('total', 0)} checks, "
        f"{summary.get('passed', 0)} ok[/]"
    )
    actionable = record.get("actionable_failures") or []
    if not actionable:
        lines.append("[neo.ok]no actionable failures[/]")
        return "\n".join(lines)
    lines.append(f"[neo.error]{summary.get('actionable', 0)} actionable failure(s)[/]")
    for row in actionable:
        lines.append(f"[neo.error]{dash} {escape(str(row.get('label') or ''))}[/]")
        lines.append(
            f"[neo.muted]   {escape(str(row.get('reason') or ''))} {dash} "
            f"{escape(str(row.get('evidence') or ''))}[/]"
        )
        remediation = row.get("remediation")
        if remediation:
            lines.append(f"[neo.muted]   fix: {escape(str(remediation))}[/]")
    return "\n".join(lines)
    lines.append(f"[neo.error]{summary.get('actionable', 0)} actionable failure(s)[/]")
    for row in actionable:
        lines.append(f"[neo.error]• {row.get('label')}[/]")
        lines.append(f"[neo.muted]   {row.get('reason')} — {row.get('evidence')}[/]")
        remediation = row.get("remediation")
        if remediation:
            lines.append(f"[neo.muted]   fix: {remediation}[/]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# `neo doctor` — the operator surface
# ---------------------------------------------------------------------------


def doctor_record(
    repo_path: Optional[str] = None, log_root: Optional[str] = None
) -> Dict[str, Any]:
    """Run every check and redact the record end to end.

    ``run_doctor`` is the raw collection; this is the operator-facing one. The
    only difference is redaction: every string in the record passes through
    ``shared.security.redact_text`` on the way out, so ``neo doctor --json``
    cannot become the surface that prints a credential a probe happened to
    capture. It returns the same document shape, so every existing consumer of
    ``run_doctor`` keeps working unchanged.
    """
    return _redact_deep(run_doctor(repo_path=repo_path, log_root=log_root))


def _redact_deep(value: Any) -> Any:
    """Recursively redact every string in a JSON-shaped structure."""
    from shared.security import redact_text

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_deep(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# `neo support-bundle` — one archive a user can attach to an issue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupportBundle:
    """The gathered support material plus where it was written.

    ``files`` maps a bundle-relative name to the exact text that was written, so
    a test (and a reviewer) can assert on the CONTENT rather than on the
    absence of a symptom. ``archive`` is the zip path when one was written, and
    empty when the caller asked for the directory form.
    """

    created_at: str
    files: Dict[str, str]
    archive: str = ""
    root: str = ""
    redaction: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-compatible, secret-free description of the bundle."""
        return {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "created_at": self.created_at,
            "archive": self.archive,
            "root": self.root,
            "file_count": len(self.files),
            "files": sorted(self.files),
            "bytes": sum(len(text.encode("utf-8")) for text in self.files.values()),
            "redaction": self.redaction,
        }


def _bundle_environment() -> Dict[str, Any]:
    """Record the interpreter/platform and the NAMES of relevant env vars.

    Values are never recorded. A support request needs to know that
    ``NEO_MODEL`` is set, not what it says; and a credential-shaped value that
    happened to sit in the environment must never travel inside a file someone
    is about to attach to a public issue.
    """
    import platform

    from shared.security import redact_text

    present = {name: bool(os.environ.get(name)) for name in _BUNDLE_ENV_NAMES}
    return (
        redact_text(
            json.dumps(
                {
                    "python_version": _python(),
                    "python_implementation": platform.python_implementation(),
                    "platform": platform.platform(),
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "neo_version": _neo_version(),
                    "env_var_names_present": present,
                    "note": "environment VALUES are deliberately not recorded",
                },
                indent=2,
                sort_keys=True,
            )
        )
        + "\n"
    )


def _neo_version() -> str:
    """Return the installed Neo version, or an honest ``unknown``."""
    try:
        from cli import main as cli_main

        return str(getattr(cli_main, "__version__", "") or "") or _pyproject_version()
    except Exception:
        return _pyproject_version()


def _pyproject_version() -> str:
    """Read the version out of pyproject.toml next to the package."""
    try:
        root = Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("version") and "=" in stripped:
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def _bundle_config_shape() -> str:
    """Record the SETTINGS SHAPE: which file declares which key, never a secret.

    One row per key with its source tier, its type, and a masked rendering.
    ``cli.neoconfig.public_value`` is the masking authority for endpoints and
    keys, and ``shared.security.redact_text`` is applied on top, so a hand-edited
    tier that bypasses the maskers still cannot put a raw credential in here.
    """
    from cli.neoconfig import merged_settings, public_value, redact_text

    try:
        merged = merged_settings()
    except Exception as exc:
        return (
            redact_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2))
            + "\n"
        )
    rows: Dict[str, Any] = {}
    for key in sorted(merged):
        value = merged[key]
        rows[str(key)] = {
            "type": type(value).__name__,
            "shape": _value_shape(value),
            "public": public_value(str(key), value)
            if _is_public_safe(str(key))
            else "withheld",
        }
    chain: List[str] = []
    for label, path in (
        ("global", None),
        ("project", None),
        ("local", None),
    ):
        del label, path
        break
    try:
        from cli.neoconfig import project_settings_dir

        project = project_settings_dir()
    except Exception:
        project = None
    chain = [str(p) for p in _settings_chain_paths(project)]
    return (
        redact_text(
            json.dumps(
                {"chain": chain, "keys": rows},
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        + "\n"
    )


#: Keys whose values are safe to render after the shared masker. Everything
#: else is ``withheld`` regardless of shape, so a new key is private by default.
_PUBLIC_KEYS = frozenset(
    {
        "model",
        "provider",
        "budget_cap_usd",
        "max_retries",
        "log_verbosity",
        "plan_preview",
        "max_turns",
        "approval",
        "log_root",
        "theme",
        "mcp_require_permissions",
    }
)


def _is_public_safe(key: str) -> bool:
    """Return whether a settings key's value may be rendered verbatim."""
    return key in _PUBLIC_KEYS


def _value_shape(value: Any) -> str:
    """Return a coarse shape for a settings value (never the value itself)."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return f"string(len={len(value)})"
    if isinstance(value, dict):
        return f"table({len(value)} keys)"
    if isinstance(value, (list, tuple)):
        return f"list({len(value)} items)"
    return type(value).__name__


def _settings_chain_paths(project: Optional[Path]) -> List[Path]:
    """Return the settings-chain files that exist, low precedence first."""
    out: List[Path] = []
    try:
        from cli.neoconfig import global_settings_path, legacy_settings_path

        out.append(legacy_settings_path())
        out.append(global_settings_path())
        if project is not None:
            out.append(project / "settings.toml")
            out.append(project / "settings.local.toml")
    except Exception:
        return out
    return [path for path in out if path.exists()]


def _bundle_connectors(repo_path: Optional[str]) -> str:
    """Record each connector's DECLARED permissions (no launch commands)."""
    from cli.connectors import ConnectorError, read_permissions
    from shared.security import redact_text

    try:
        declared = read_permissions(repo_path)
    except ConnectorError as exc:
        return (
            redact_text(
                json.dumps({"error": f"permission file unreadable: {exc}"}, indent=2)
            )
            + "\n"
        )
    except Exception as exc:
        return (
            redact_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2))
            + "\n"
        )
    payload = {label: permission.to_dict() for label, permission in declared.items()}
    return redact_text(json.dumps(payload, indent=2, sort_keys=True)) + "\n"


def _bundle_hooks(repo_path: Optional[str]) -> str:
    """Record the merged hook config and the fail-policy table actually in force.

    A hook config error is a support case, so the error is recorded rather than
    swallowed: this is the surface that shows an operator their config is
    unparseable and which file is at fault.
    """
    from shared.security import redact_text

    try:
        from extensions.user_hooks import load_hook_config

        config = load_hook_config(repo_path=repo_path)
        payload = config.to_dict()
    except Exception as exc:
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    return (
        redact_text(json.dumps(payload, indent=2, sort_keys=True, default=str)) + "\n"
    )


def _bundle_plugins() -> str:
    """Record installed plugins and their install records (no file contents)."""
    from shared.security import redact_text

    try:
        from cli import plugins as plugins_mod

        rows = []
        for entry in plugins_mod.list_plugins():
            record = dict(entry)
            receipt = plugins_mod.read_install_receipt(str(entry.get("name") or ""))
            record["install_record"] = receipt.to_dict() if receipt else None
            rows.append(record)
        payload = {"root": str(plugins_mod.plugins_root()), "plugins": rows}
    except Exception as exc:
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    return (
        redact_text(json.dumps(payload, indent=2, sort_keys=True, default=str)) + "\n"
    )


def _bundle_recent_errors(log_root: Optional[str], runs: int) -> str:
    """Read the most recent run journals and keep the error-class rows.

    Bounded on three axes so a bundle is a diagnostic and not an archive: at
    most ``runs`` run directories, at most
    ``_BUNDLE_ERROR_LIMIT_PER_EVENT`` rows per event name, and every row is
    truncated before it is written. A malformed journal line is skipped, never
    fatal — a corrupt journal is exactly the kind of thing this bundle exists to
    surface.
    """
    from shared.security import redact_text

    try:
        root = Path(log_root) if log_root else default_logs_dir()
    except Exception:
        return '{"error": "log root unavailable"}\n'
    if not root.is_dir():
        return (
            json.dumps({"log_root": str(root), "runs_scanned": 0, "errors": []}) + "\n"
        )
    try:
        candidates = sorted(
            (entry for entry in root.iterdir() if entry.is_dir()),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )[: max(1, int(runs))]
    except OSError as exc:
        return redact_text(json.dumps({"error": str(exc)})) + "\n"
    collected: Dict[str, List[Dict[str, Any]]] = {}
    scanned = 0
    for directory in candidates:
        for journal in sorted(directory.glob("*.jsonl")):
            scanned += 1
            if scanned > _BUNDLE_MAX_RUNS_SCANNED:
                break
            try:
                lines = journal.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            for line in lines[-400:]:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                name = str(row.get("event") or row.get("kind") or row.get("type") or "")
                if name not in _BUNDLE_ERROR_EVENTS:
                    continue
                bucket = collected.setdefault(name, [])
                if len(bucket) >= _BUNDLE_ERROR_LIMIT_PER_EVENT:
                    continue
                bucket.append(
                    {
                        "run": directory.name,
                        "journal": journal.name,
                        "event": name,
                        "detail": truncate_text(
                            json.dumps(
                                row.get("payload")
                                if row.get("payload") is not None
                                else row.get("data"),
                                default=str,
                            ),
                            2_000,
                        ),
                    }
                )
    payload = {
        "log_root": str(root),
        "runs_scanned": len(candidates),
        "journals_scanned": scanned,
        "event_names_watched": list(_BUNDLE_ERROR_EVENTS),
        "limit_per_event": _BUNDLE_ERROR_LIMIT_PER_EVENT,
        "errors": collected,
    }
    return redact_text(json.dumps(payload, indent=2, sort_keys=True)) + "\n"


def truncate_text(value: str, limit: int) -> str:
    """Truncate a string to ``limit`` characters with an explicit marker."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} chars truncated]"


def build_support_bundle(
    repo_path: Optional[str] = None,
    log_root: Optional[str] = None,
    *,
    recent_runs: int = 5,
) -> SupportBundle:
    """Gather everything a maintainer needs, redacted, in memory.

    Assumes ``recent_runs`` is a small positive count (the caller clamps it);
    everything is bounded regardless. Every value written here has already been
    through ``shared.security.redact_text`` inside its own builder, and the
    manifest is redacted again on the way out, so adding a new section cannot
    accidentally make the bundle the one place a secret escapes.

    Nothing is written to disk: :func:`write_support_bundle` does that. Keeping
    the two apart is what lets a test assert on the exact bytes.
    """
    record = doctor_record(repo_path=repo_path, log_root=log_root)
    manifest_record = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "command": "support-bundle",
        "run_at": record.get("run_at", _now()),
        "repo_path": record.get("repo_path", ""),
        "log_root": record.get("log_root", ""),
        "summary": record.get("summary", {}),
        "actionable_failures": [
            {
                "key": row.get("key"),
                "status": row.get("status"),
                "reason": row.get("reason"),
                "remediation": row.get("remediation"),
            }
            for row in record.get("actionable_failures") or []
        ],
    }
    files: Dict[str, str] = {
        "doctor.json": render_doctor_json(record) + "\n",
        "doctor.txt": _strip_markup(render_doctor_human(record)) + "\n",
        "doctor-summary.json": _dumps(manifest_record),
        "environment.json": _bundle_environment(),
        "config-shape.json": _bundle_config_shape(),
        "connectors.json": _bundle_connectors(repo_path),
        "hooks.json": _bundle_hooks(repo_path),
        "plugins.json": _bundle_plugins(),
        "recent-errors.json": _bundle_recent_errors(log_root, recent_runs),
    }
    created = _now()
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at": created,
        "redaction": "shared.security.redact_text applied to every value",
        "contents": {
            name: {"sha256": _sha256(text), "bytes": len(text.encode("utf-8"))}
            for name, text in sorted(files.items())
        },
    }
    files["manifest.json"] = _dumps(manifest)
    return SupportBundle(
        created_at=created,
        files=_redact_deep(files),
        redaction=manifest["redaction"],
    )


def _dumps(value: Any) -> str:
    """Serialize a JSON-shaped value deterministically and redacted."""
    from shared.security import redact_text

    return redact_text(json.dumps(value, indent=2, sort_keys=True, default=str)) + "\n"


def _sha256(text: str) -> str:
    """Return the hex SHA-256 of a text payload."""
    import hashlib

    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _strip_markup(text: str) -> str:
    """Remove rich markup tags from the human doctor render.

    The bundle is read by a human in a text editor or an issue tracker, where
    ``[neo.ok]`` is noise; the JSON document beside it is the machine surface.
    """
    import re as _re

    return _re.sub(r"\[/?[a-zA-Z0-9_. ]+\]", "", str(text or ""))


def write_support_bundle(
    bundle: SupportBundle, out: str, *, archive: bool = True
) -> str:
    """Write the bundle to ``out`` and return the path actually created.

    ``archive=False`` writes a plain directory of files, which is what a user
    wants when they are about to hand-pick files. ``archive=True`` (the
    default) writes a single ``.zip``, which is what "one archive a user can
    attach to an issue" means.

    The zip is written through a unique temp name and moved into place, so an
    interrupted write cannot leave a truncated archive that looks complete.
    """
    target = Path(out)
    if not archive:
        target.mkdir(parents=True, exist_ok=True)
        for name, text in sorted(bundle.files.items()):
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        bundle = SupportBundle(
            created_at=bundle.created_at,
            files=bundle.files,
            archive="",
            root=str(target),
            redaction=bundle.redaction,
        )
        return str(target)
    import zipfile

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    try:
        with zipfile.ZipFile(
            tmp, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive_file:
            for name, text in sorted(bundle.files.items()):
                # A fixed timestamp keeps two bundles of the same state
                # byte-comparable, which is what makes "did anything change?"
                # answerable from the archive alone.
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive_file.writestr(info, text)
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return str(target)


def render_support_bundle_manifest(bundle: SupportBundle) -> str:
    """Return the machine-readable description of a bundle."""
    return _dumps(bundle.to_dict())
