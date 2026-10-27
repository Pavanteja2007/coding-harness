"""Run this project's verification commands and tee their RAW output to disk.

**Why this module exists.** ``runtime/AGENTS.md`` has, across nine prior rounds,
recorded the same gap: the in-package ``runtime/test_*.py`` modules are not
collected by a bare ``python -m pytest`` (``pyproject.toml`` pins
``testpaths = ["tests"]``), so every round re-runs its own commands by hand and
the evidence lives in chat scrollback — which is unsearchable, gone next session,
and impossible to diff against a later round.

This is the artifact that fixes that half. It is not a test runner and it makes
no judgement: it runs a declared command list, writes every byte of every
command's output to one file, and records each exit code.

**The rule it exists to enforce: a red command is recorded, not hidden.** A
verification driver that stops on the first failure leaves the reader with no
record of the commands after it, and that is precisely how a round ends up
reporting a partial lane as if it were a whole one. So this driver runs
everything, then reports. Its own exit code is about whether the EVIDENCE was
captured, not whether the evidence was green.

    python -m runtime.verification_log                     # the declared list
    python -m runtime.verification_log --list              # show it, run nothing
    python -m runtime.verification_log --out C:/tmp/raw.txt

Nothing here needs Docker, a provider, or a credential. Each command is declared
with its argv, so adding one is a data edit rather than a code edit.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "VERIFICATION_COMMANDS",
    "child_env",
    "format_summary",
    "run_verification",
]

_PY = sys.executable


def _pytest(*targets: str) -> List[str]:
    return [_PY, "-m", "pytest", *targets, "-q", "-p", "no:randomly"]


#: The declared command list. Every entry names what it is, so a reader of the
#: artifact never has to guess which of twenty pytest invocations produced a line.
VERIFICATION_COMMANDS: List[Dict[str, Any]] = [
    {
        "name": "in-package cache pins (Wave 2, this round)",
        "argv": _pytest("runtime/test_p3b_w2_pins.py"),
    },
    {
        "name": "in-package cache pins (Wave 1, regression)",
        "argv": _pytest("runtime/test_p3b_w1_pins.py"),
    },
    {
        "name": "both in-package cache modules together (interference check)",
        "argv": _pytest("runtime/test_p3b_w2_pins.py", "runtime/test_p3b_w1_pins.py"),
    },
    {
        "name": "tests/test_prompt_cache_cost.py",
        "argv": _pytest("tests/test_prompt_cache_cost.py"),
    },
    {
        "name": "tests/test_model_router.py + tests/test_provider_smoke.py",
        "argv": _pytest("tests/test_model_router.py", "tests/test_provider_smoke.py"),
    },
    {
        "name": "budget/quota + resilience",
        "argv": _pytest(
            "tests/test_ceiling_r2_14_budget_quota.py", "tests/test_ceiling14_resilience.py"
        ),
    },
    {
        "name": (
            "local-model coverage "
            "(NOTE: tests/test_local_models.py does not exist; this is the real file)"
        ),
        "argv": [_PY, "-m", "pytest", "tests/test_ceiling14_resilience.py", "-q",
                 "-p", "no:randomly", "-k", "local or target or dialect"],
    },
    {
        "name": "wider cache/cost surface",
        "argv": _pytest(
            "tests/test_ceiling_r2_13_capability_routing.py", "tests/test_streaming_ui.py"
        ),
    },
    {
        "name": "honesty invariants",
        "argv": [_PY, "-m", "runtime.invariants"],
    },
    {
        "name": "cache prefix audit (5 runs)",
        "argv": [_PY, "-m", "runtime.cache_prefix_audit", "--runs", "5"],
    },
    {
        "name": "ruff check (this round's files)",
        "argv": [
            _PY, "-m", "ruff", "check", "runtime/prompt_cache.py",
            "runtime/cache_economics.py", "runtime/cache_prefix_audit.py",
            "runtime/test_p3b_w2_pins.py", "runtime/verification_log.py",
        ],
    },
    {
        "name": "ruff check runtime (whole package -- the recorded baseline)",
        "argv": [_PY, "-m", "ruff", "check", "runtime"],
    },
    {
        "name": "compileall -q runtime",
        "argv": [_PY, "-m", "compileall", "-q", "runtime"],
    },
    {
        "name": "git diff --check (scoped to this round's tracked files)",
        "argv": ["git", "diff", "--check", "--", "runtime/prompt_cache.py",
                 "runtime/cache_economics.py"],
    },
    {
        "name": "prompt-regression host self-check",
        "argv": [_PY, "-m", "evals.run", "--check"],
    },
    {
        "name": "trust ladder",
        "argv": [_PY, "-m", "evals.run", "--suite", "trust-ladder"],
    },
]


def child_env() -> Dict[str, str]:
    """Return the environment every child command runs with.

    ``NEO_PRELOAD_LITELLM=off`` is not cosmetic. With it unset, importing
    ``runtime`` starts a background ``litellm`` preload thread; any test that
    installs its own fake ``litellm`` module then races that thread, and the
    race resolves as a **real network call** from a test that believes it is
    offline. That is how the first measurements of Phase 3b/W2 were lost, and
    the fix is one environment variable rather than a lock.
    """
    env = dict(os.environ)
    env["NEO_PRELOAD_LITELLM"] = "off"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _printable(argv: Sequence[str]) -> str:
    return " ".join(
        (arg.replace(_PY, "python") if arg == _PY else arg) for arg in argv
    )


def run_verification(
    *,
    out_path: Optional[str] = None,
    commands: Optional[Sequence[Dict[str, Any]]] = None,
    timeout_s: float = 2400.0,
    log_dir: str = "logs/verification",
) -> Dict[str, Any]:
    """Run every declared command, tee the raw output, and return a summary.

    Never raises for a failing, missing, or hanging command. Each result carries
    its ``exit_code``, its wall time, and whether it passed, so the summary and
    the artifact cannot disagree.
    """
    entries = list(commands if commands is not None else VERIFICATION_COMMANDS)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = out_path or os.path.join(log_dir, f"verification-{stamp}.txt")
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)

    summary: List[Dict[str, Any]] = []
    started = time.perf_counter()
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(
            "raw verification output\n"
            f"host      {platform.platform()}\n"
            f"python    {platform.python_version()}\n"
            f"cpus      {os.cpu_count()}\n"
            f"recorded  {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "note      a non-zero exit below is RECORDED, not hidden; the exit "
            "code of THIS driver reports whether the evidence was captured\n"
            + "=" * 78
            + "\n"
        )
        for entry in entries:
            argv = [str(arg) for arg in entry["argv"]]
            label = str(entry.get("name") or _printable(argv))
            handle.write(f"\n### {label}\n$ {_printable(argv)}\n")
            handle.flush()
            began = time.perf_counter()
            record: Dict[str, Any] = {
                "name": label,
                "command": _printable(argv),
            }
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    env=child_env(),
                    timeout=timeout_s,
                )
                output = (proc.stdout or "") + (proc.stderr or "")
                record["exit_code"] = proc.returncode
            except subprocess.TimeoutExpired:
                output = f"TIMEOUT after {timeout_s}s"
                record["exit_code"] = None
                record["timed_out"] = True
            except FileNotFoundError as missing:
                output = f"COMMAND NOT FOUND: {missing}"
                record["exit_code"] = None
                record["missing"] = True
            record["elapsed_s"] = round(time.perf_counter() - began, 1)
            record["output_chars"] = len(output)
            record["passed"] = record["exit_code"] == 0
            handle.write(output)
            if not output.endswith("\n"):
                handle.write("\n")
            handle.write(f"[exit {record['exit_code']}] {record['elapsed_s']}s\n")
            handle.flush()
            summary.append(record)

        totals = {
            "commands": len(summary),
            "passed": sum(1 for row in summary if row["passed"]),
            "failed": sum(1 for row in summary if row["passed"] is False),
            "elapsed_s": round(time.perf_counter() - started, 1),
        }
        handle.write("\n" + "=" * 78 + "\n")
        handle.write(f"TOTALS {totals}\n")
        for row in summary:
            handle.write(
                f"  exit={row['exit_code']!s:>4}  {row['elapsed_s']}s  {row['name']}\n"
            )
        handle.write(f"\nwritten to {destination}\n")

    return {
        "available": True,
        "out_path": destination,
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
        },
        "totals": totals,
        "commands": summary,
        "failed_commands": [row["name"] for row in summary if row["passed"] is False],
        "note": (
            "a non-zero exit is recorded, not hidden; the two known "
            "non-runtime reds are attributed in runtime/AGENTS.md"
        ),
    }


def format_summary(payload: Dict[str, Any]) -> str:
    """Render the summary a human reads, with every exit code visible."""
    lines = ["", f"wrote {payload['out_path']}", f"totals: {payload['totals']}"]
    for row in payload["commands"]:
        mark = "ok  " if row["passed"] else "RED "
        lines.append(
            f"  {mark} exit={row['exit_code']!s:>4} {row['elapsed_s']:>7}s  "
            f"{row['name']}"
        )
    if payload["failed_commands"]:
        lines.append("")
        lines.append("recorded reds (attribution lives in runtime/AGENTS.md):")
        for name in payload["failed_commands"]:
            lines.append(f"  - {name}")
    return "\n".join(lines)


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m runtime.verification_log",
        description="Run the declared verification commands and tee their raw output.",
    )
    parser.add_argument("--out", default=None, help="output path")
    parser.add_argument("--list", action="store_true", help="list and exit")
    parser.add_argument("--timeout", type=float, default=2400.0)
    args = parser.parse_args(argv)

    if args.list:
        for entry in VERIFICATION_COMMANDS:
            print(f"{entry.get('name')}\n  $ {_printable(entry['argv'])}")
        return 0

    payload = run_verification(out_path=args.out, timeout_s=args.timeout)
    print(format_summary(payload))
    # 0 whenever the evidence was captured. A red COMMAND is the finding; this
    # driver's exit code is about the recording.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
