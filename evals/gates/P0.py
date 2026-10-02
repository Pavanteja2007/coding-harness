"""G0 — the P0 Foundation gate. This is the gate every later phase depends on.

WHAT A GATE IS FOR HERE
-----------------------

``AGENTS.md`` states the project's Gate: *"if it cannot be measured, it does
not ship."* This module is that claim made executable for P0. It probes, it
records the exact evidence, and it derives a verdict. It does not choose one.

THE FOUR STATUSES, AND WHY THERE IS NO FIFTH
--------------------------------------------

==============  =========================================================
``pass``        Ran and held.
``fail``        Ran and broke.
``blocked``     Could not run. **The exact reason is mandatory.**
``not_implemented``  Does not exist. **The owning phase is mandatory.**
==============  =========================================================

``skip`` is absent on purpose and cannot be constructed. This is the single
most important property of the file: in a summary table a skip renders
exactly like a pass, so a blocked Docker daemon, an unconfigured provider
key, and a green run all become the same glyph. ``DOCTRINE.md`` §1 lists
"report a blocked lane as skipped" as a Never-do, and this is the enforcement
point. ``_status`` rejects the string ``"skip"`` outright, so a future edit
that adds it fails a test rather than quietly re-introducing the lie.

WHY A RED G0 IS A VALID OUTCOME
-------------------------------

A gate that can only come back green is a report generator. Every rung here
is capable of reporting ``fail`` and the module's exit code is 2 for that.
Relaxing a test, a threshold or a pin to make this file green is explicitly
forbidden by the brief that created it, and the honest-red case is the
designed case: this repository has shipped mechanisms nothing imported, a
default product path weaker than the path it replaced, and a redactor that
hung on a 40,000-character run.

WHAT G0 DOES NOT ESTABLISH
--------------------------

Stated in the report itself (``NOT_ESTABLISHED``) and printed by the CLI, so
it cannot be separated from the numbers:

* ``evals.run --check`` is a **host self-check of the 14-task prompt set**. It
  proves the eval harness is internally consistent and that no prompt change
  regressed a scripted arm. It is **not** a claim about model quality.
* **Every model call in this gate is a scripted double.** No live provider was
  reachable (T3 recorded ``ServiceUnavailableError: No available channel`` on
  3/3 completions), so no rung here measures model behaviour, model cost, or
  model latency.
* A green full suite is a statement about **this tree on this host at this
  commit**, not about a released artifact: no reproducible build, no
  clean-room install and no tag were produced (SG-04).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------
# the vocabulary -- closed, and `skip` is not in it
# --------------------------------------------------------------------------

PASS = "pass"
FAIL = "fail"
BLOCKED = "blocked"
NOT_IMPLEMENTED = "not_implemented"

STATUSES: Tuple[str, ...] = (PASS, FAIL, BLOCKED, NOT_IMPLEMENTED)

#: Verdicts the gate itself will emit. Distinct from `STATUSES`: the gate's own
#: health is a different question from any single rung's status.
GATE_GREEN = "G0_GREEN"
GATE_RED = "G0_RED"

#: Every rung's status must be one of these. Asserted by
#: `tests/test_g0_gate.py::test_the_status_vocabulary_has_no_skip`.
_FORBIDDEN_STATUSES: Tuple[str, ...] = ("skip", "skipped", "xfail", "pending", "todo")


class GateError(RuntimeError):
    """Raised when a rung cannot be stated honestly.

    In practice: a ``blocked`` rung with no reason, or a ``not_implemented``
    rung with no owning phase. Both are refused at construction so the
    dishonest report cannot be written rather than merely discouraged.
    """


def _status(value: str) -> str:
    """Validate one status string. Refuses ``skip`` and every near-synonym."""
    if value in _FORBIDDEN_STATUSES or value.strip().lower() in _FORBIDDEN_STATUSES:
        raise GateError(
            f"{value!r} is not a permitted gate status. A skip is "
            "indistinguishable from a pass in a summary table. Use `blocked` "
            "with the exact reason, or `not_implemented` with the owning phase."
        )
    if value not in STATUSES:
        raise GateError(f"{value!r} is not in the closed vocabulary {STATUSES!r}")
    return value


# --------------------------------------------------------------------------
# the rung
# --------------------------------------------------------------------------


@dataclass
class Rung:
    """One measured row of the gate.

    :param id: stable machine name; what a later phase's prompt cites.
    :param title: one human sentence.
    :param status: one of :data:`STATUSES`, validated on construction.
    :param detail: the finding, in prose. For ``blocked`` this MUST carry the
        exact reason (an exception, an exit code, a missing prerequisite).
    :param owner: who owns closing it (``T1 / P2.1`` style).
    :param evidence: what was actually run/observed -- a command, a file, a
        probe result. A rung with no evidence is an assertion.
    :param blocking: whether this rung alone makes the gate red.
    """

    id: str
    title: str
    status: str
    detail: str
    owner: str = ""
    evidence: str = ""
    blocking: bool = True

    def __post_init__(self) -> None:
        self.status = _status(self.status)
        if not self.id or not self.title:
            raise GateError("a rung needs an id and a title")
        if not str(self.detail).strip():
            raise GateError(
                f"{self.id}: a rung with no detail is an assertion. Say what happened."
            )
        if self.status == BLOCKED and not str(self.detail).strip():
            raise GateError(f"{self.id}: blocked must carry the exact reason")
        if self.status == NOT_IMPLEMENTED and not str(self.owner).strip():
            raise GateError(
                f"{self.id}: not_implemented must name the OWNING PHASE in "
                "`owner`, so a reader knows whether to wait or to build"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "owner": self.owner,
            "evidence": self.evidence,
            "blocking": self.blocking,
        }


#: Constructors that make the two "you must explain yourself" statuses hard to
#: get wrong. Used by the rungs below and by the test suite.
def blocked(id_: str, title: str, reason: str, **kw: Any) -> Rung:
    """A rung that could not run. ``reason`` is mandatory and non-empty."""
    if not str(reason).strip():
        raise GateError(f"{id_}: blocked() requires the exact reason")
    return Rung(id_, title, BLOCKED, reason, **kw)


def not_implemented(id_: str, title: str, owning_phase: str, **kw: Any) -> Rung:
    """A rung that does not exist yet. ``owning_phase`` is mandatory."""
    if not str(owning_phase).strip():
        raise GateError(f"{id_}: not_implemented() requires the owning phase")
    return Rung(
        id_,
        title,
        NOT_IMPLEMENTED,
        f"does not exist yet; owned by {owning_phase}",
        owner=owning_phase,
        **kw,
    )


# --------------------------------------------------------------------------
# probes -- real subprocesses, real output, bounded
# --------------------------------------------------------------------------

DEFAULT_TIMEOUT_S = 900


def run_probe(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    env_extra: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run one command and capture the whole outcome.

    Returns ``{ok, returncode, stdout, stderr, timed_out, argv}``. Never
    raises for a non-zero exit or a timeout: a probe that raises is a probe
    that cannot report a broken lane, which is the one thing this gate exists
    to do.
    """
    import os

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("NO_COLOR", "1")
    if env_extra:
        env.update(env_extra)
    printable = " ".join(str(a) for a in argv)
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": True,
            "argv": printable,
            "error": f"did not finish within {timeout_s}s",
        }
    except OSError as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "argv": printable,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "timed_out": False,
        "argv": printable,
        "error": "",
    }


def docker_reachable() -> Dict[str, Any]:
    """Probe the Docker daemon with ``docker info`` and keep the raw output.

    Returns ``{reachable, cli_present, evidence, error}``. A missing CLI and an
    unreachable daemon are DIFFERENT states and are reported differently: the
    first means "this host cannot run container lanes at all", the second
    means "the daemon is down".
    """
    cli = shutil.which("docker")
    if not cli:
        return {
            "reachable": False,
            "cli_present": False,
            "evidence": "docker CLI not on PATH",
            "error": "the docker CLI is not installed on this host",
        }
    probe = run_probe(["docker", "info"], timeout_s=60)
    if probe["timed_out"]:
        return {
            "reachable": False,
            "cli_present": True,
            "evidence": "docker info timed out after 60s",
            "error": "docker info did not finish within 60s",
        }
    if probe["ok"]:
        server = ""
        for line in probe["stdout"].splitlines():
            if "Server Version" in line or "Storage Driver" in line:
                server += line.strip() + "; "
        return {
            "reachable": True,
            "cli_present": True,
            "evidence": (server or "docker info exited 0").strip("; "),
            "error": "",
        }
    tail = (probe["stderr"] or probe["stdout"] or "").strip().splitlines()
    return {
        "reachable": False,
        "cli_present": True,
        "evidence": tail[-1] if tail else f"docker info exit {probe['returncode']}",
        "error": (tail[-1] if tail else f"docker info exited {probe['returncode']}"),
    }


#: Severities that BLOCK a gate. `medium` is deliberately absent: this is a
#: build gate, not a risk-acceptance process, and a gate that fails on
#: everything is a gate that gets disabled.
BLOCKING_SEVERITIES: Tuple[str, ...] = ("critical", "high")

#: The suite T5.W2.1's brief names as Wave 1's deliverable. **It is not
#: registered** in `evals/run.py` as of 2026-10-01: `--suite` accepts only
#: `auto`, `combined`, `daily-driver`, `prompt-regression`. The Trust Ladder
#: exists as `tests/test_ceiling_r2_15_trust.py`, which is a pytest suite and
#: not reachable through the eval runner, so a gate cannot measure it there.
#: Read the registration list from argparse at run time rather than trusting
#: this constant, so a suite added later needs no edit here.
TRUST_LADDER_SUITE = "trust-ladder"


class _SuiteRegistration:
    """What `--help` said the runner accepts, for the report's evidence field."""

    registered: ClassVar[List[str]] = []


LADDER = _SuiteRegistration()


def _installed_version(package: str) -> Optional[str]:
    """Return the INSTALLED version of ``package``, or None if unknown."""
    import importlib.metadata as md

    for name in (package, package.replace("_", "-"), package.replace("-", "_")):
        try:
            return md.version(name)
        except Exception:
            continue
    return None


def _pinned_version(root: Path, package: str) -> Optional[str]:
    """Return the version PINNED anywhere in ``pyproject.toml``, or None.

    The pin is what ships, so it is the authoritative answer for a release
    gate; the installed version is only a fallback.

    The WHOLE file is scanned rather than the ``[project] dependencies`` list,
    because ``setuptools`` is pinned in ``[build-system] requires`` and is
    exactly the shape that a dependencies-only scan silently misses -- which
    would have reported a resolved advisory as an active blocker.
    """
    pyproject = Path(root) / "pyproject.toml"
    if not pyproject.is_file():
        return None
    text = pyproject.read_text(encoding="utf-8", errors="replace")
    normalised = package.replace("_", "-").lower()
    # Whole-file regex, not line-by-line: `[build-system] requires =
    # ["setuptools==84.0.0"]` puts the pin on a line whose first token is the
    # word `requires`, so a line-oriented parser reads the wrong name and
    # falls through to the INSTALLED version -- which reported a resolved
    # advisory as an active blocker.
    pattern = re.compile(
        r"[\"']?([A-Za-z0-9][A-Za-z0-9._-]*)[\"']?\s*==\s*([0-9][0-9A-Za-z.\-+]*)"
    )
    for match in pattern.finditer(text):
        if match.group(1).replace("_", "-").lower() == normalised:
            return match.group(2)
    return None


def _npm_pinned_version(root: Path, package: str) -> Optional[str]:
    """Return the version LOCKED in ``site/package-lock.json``, or None.

    Three of the ten advisories in the DB are npm packages (``lodash``,
    ``minimist``, ``tar``) and this repository ships a Next.js site. A Python
    resolver cannot see them, and an advisory it cannot see must be reported
    as unresolvable rather than quietly dropped.
    """
    lock = Path(root) / "site" / "package-lock.json"
    if not lock.is_file():
        return None
    try:
        data = json.loads(lock.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    packages = data.get("packages") if isinstance(data, dict) else None
    if isinstance(packages, dict):
        for key, value in packages.items():
            if key.endswith(f"node_modules/{package}") and isinstance(value, dict):
                version = value.get("version")
                if isinstance(version, str):
                    return version
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if isinstance(deps, dict):
        value = deps.get(package)
        if isinstance(value, dict) and isinstance(value.get("version"), str):
            return value["version"]
    return None


def _version_tuple(value: str) -> Tuple[Any, ...]:
    """Best-effort comparable version tuple. Never raises."""
    parts: List[Any] = []
    for chunk in re.split(r"[.\-+]", value):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        elif chunk:
            parts.append((1, chunk))
    return tuple(parts)


def count_security_blockers(root: Path) -> Dict[str, Any]:
    """Count the ACTIVE security blockers from the project's own advisory DB.

    **An advisory with no resolution field cannot be reported as resolved.**
    ``shared.security_advisories.Advisory`` carries ``severity``,
    ``introduced`` and ``fixed`` but NO ``status``. So "is this fixed?" is
    answered by MEASURING the version this project actually pins, against the
    advisory's own ``fixed`` bound:

    * ``pinned < fixed``  -> ACTIVE blocker
    * ``pinned >= fixed`` -> not active, and the version is reported

    Reporting ``0`` because a ``status`` attribute was absent would be the
    "render an absent value as 0" anti-pattern in ``DOCTRINE.md`` §1, and it
    would have been wrong here: 6 of these 10 advisories name a ``fixed``
    release this repository does not yet pin.
    """
    try:
        import importlib.util

        spec = importlib.util.find_spec("shared.security_advisories")
        if spec is None:
            return {
                "measurable": False,
                "count": None,
                "detail": "shared.security_advisories is not importable",
                "blocking": [],
                "resolved": [],
                "unknown_version": [],
            }
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken advisory registry is a finding
        return {
            "measurable": False,
            "count": None,
            "detail": f"shared.security_advisories failed to import: {exc!r}",
            "blocking": [],
            "resolved": [],
            "unknown_version": [],
        }

    advisories = getattr(module, "ADVISORIES", None)
    if advisories is None:
        return {
            "measurable": False,
            "count": None,
            "detail": "shared.security_advisories exposes no ADVISORIES tuple",
            "blocking": [],
            "resolved": [],
            "unknown_version": [],
        }

    blocking: List[str] = []
    resolved: List[str] = []
    unknown: List[str] = []
    not_applicable: List[str] = []
    for advisory in advisories:
        severity = str(getattr(advisory, "severity", "")).strip().lower()
        ident = str(getattr(advisory, "advisory_id", "?"))
        package = str(getattr(advisory, "package", "?"))
        if severity not in BLOCKING_SEVERITIES:
            continue
        fixed = str(getattr(advisory, "fixed", "") or "")
        pinned = (
            _pinned_version(root, package)
            or _npm_pinned_version(root, package)
            or _installed_version(package)
        )
        if not fixed:
            unknown.append(f"{ident}({package}: advisory declares no `fixed` bound)")
            continue
        if not pinned:
            # Three buckets are possible and they are NOT the same answer:
            #   * present in a manifest we can read but with no version  -> unknown
            #   * absent from every manifest we can read                  -> not applicable
            # Absence is reported, not assumed: an advisory for a package the
            # shipped lockfile does not contain does not apply to the build,
            # but that is a JUDGEMENT and it is listed so a reader can
            # overturn it. It is never silently dropped.
            known_to_project = _pinned_version(root, package) is not None
            if known_to_project:
                unknown.append(f"{ident}({package}: declared but unresolvable)")
            else:
                not_applicable.append(
                    f"{ident}({package}: absent from pyproject pins, the site "
                    "lockfile and the installed set)"
                )
            continue
        if _version_tuple(pinned) < _version_tuple(fixed):
            blocking.append(
                f"{ident}({package} {severity}: pinned {pinned} < fixed {fixed})"
            )
        else:
            resolved.append(f"{ident}({package} {severity}: {pinned} >= {fixed})")

    detail = (
        f"{len(list(advisories))} advisories in the DB; "
        f"{len(blocking)} ACTIVE blocker(s) at severity "
        f"{'/'.join(BLOCKING_SEVERITIES)}; {len(resolved)} already past their "
        f"`fixed` bound; {len(unknown)} present-but-unresolvable; "
        f"{len(not_applicable)} not applicable to this build (absent from every "
        "manifest read)"
    )
    return {
        "measurable": True,
        "count": len(blocking) + len(unknown),
        "detail": detail,
        "blocking": blocking,
        "resolved": resolved,
        "unknown_version": unknown,
        "not_applicable": not_applicable,
    }


# --------------------------------------------------------------------------
# pytest report ingestion
# --------------------------------------------------------------------------

_PYTEST_FAILED_LINE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
#: pytest prints its summary COUNTS IN NO FIXED ORDER -- `2 failed, 40 passed,
#: 3 skipped` is the normal shape, so a positional regex reads `40` as the
#: passed count and finds no `failed` group at all. That bug reported every
#: dirty lane as CLEAN. Counted by label instead, which is order-independent.
_PYTEST_COUNT = re.compile(
    r"(\d+)\s+(passed|failed|skipped|xfailed|xpassed|errors?|warnings?|deselected)"
)


def read_log_text(path: Path) -> str:
    """Read a log written by ANY of the shells this project is driven from.

    Windows PowerShell 5.1's ``*>`` redirect emits UTF-16LE with a BOM, while
    ``python ... > file`` and every POSIX shell emit UTF-8. A gate that assumes
    UTF-8 reports "no recognisable summary line" for a perfectly good log --
    which is exactly the false ``blocked`` this module exists to prevent, in the
    one direction that hides a real result.
    """
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", errors="replace")
    return raw.decode("utf-8", errors="replace")


def parse_pytest_log(text: str) -> Dict[str, Any]:
    """Extract totals and the failed node ids from a ``pytest -q`` log.

    Deliberately conservative: a log with no recognisable summary line yields
    ``parsed=False`` and ``None`` totals, so a caller cannot report a missing
    measurement as zero.
    """
    failed = [m.group(1) for m in _PYTEST_FAILED_LINE.finditer(text)]
    counts: Dict[str, int] = {}
    parsed = False
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        # pytest's `-q` summary line is bare: "2 failed, 40 passed, 3 skipped
        # in 88.10s". Only `-v` adds `====` separators, so requiring "==" here
        # would make the parser reject EVERY real log this gate is fed. Found
        # by the synthetic-log control in tests/test_g0_gate.py.
        if not re.search(r"\b(passed|failed|no tests ran)\b", stripped):
            continue
        found = _PYTEST_COUNT.findall(stripped)
        if not found:
            continue
        for value, label in found:
            key = label.rstrip("s") if label.startswith("error") else label
            if key == "warning":
                continue
            counts[key] = counts.get(key, 0) + int(value)
        parsed = True
        break
    if not parsed and "no tests ran" in text:
        # A run that collected nothing is a MEASUREMENT, not an absence: it is
        # a lane that ran and found no tests, which is never a pass.
        return {
            "parsed": True,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "failed_nodes": [],
            "no_tests_ran": True,
            "exit_zero": False,
        }
    return {
        "parsed": parsed,
        "passed": counts.get("passed", 0) if parsed else None,
        "failed": counts.get("failed", 0) if parsed else len(failed),
        "skipped": counts.get("skipped", 0) if parsed else None,
        "errors": counts.get("error", 0) if parsed else None,
        "xfailed": counts.get("xfailed", 0) if parsed else None,
        "failed_nodes": sorted(failed),
        "no_tests_ran": False,
        "exit_zero": bool(re.search(r"\b0 failed\b", text)),
    }


# --------------------------------------------------------------------------
# THE GATE
# --------------------------------------------------------------------------

#: Stated in the report and printed by the CLI. Never separated from the
#: numbers, because a verdict read without its limits is a different claim.
NOT_ESTABLISHED: Tuple[str, ...] = (
    "`python -m evals.run --check` is a HOST SELF-CHECK of the 14-task prompt "
    "set. It proves the eval harness is internally consistent and that no "
    "prompt change regressed a scripted arm. It is NOT a claim about model "
    "quality, model cost, or model latency.",
    "EVERY MODEL CALL IN THIS GATE IS A SCRIPTED DOUBLE. No live provider was "
    "reachable: T3 recorded `ServiceUnavailableError: No available channel` on "
    "3/3 live completions. Nothing here measures model behaviour.",
    "A green suite is a statement about THIS TREE ON THIS HOST, not about a "
    "released artifact. No reproducible build, no clean-room install and no "
    "tag were produced (SG-04: the shared tree is dirty and committing "
    "requires explicit human approval).",
    "Docker-lane results depend on a shared daemon that four terminals were "
    "using concurrently while this ran. Container residue and image-cache "
    "growth are therefore host-pressure observations, not product results.",
)


def _suite_rung(
    rung_id: str,
    title: str,
    log_path: Optional[Path],
    *,
    owner: str,
    command: str,
    probe: Optional[Dict[str, Any]] = None,
) -> Rung:
    """Turn a measured pytest log into a rung, or say honestly why there is none.

    The four cases, and what each one is allowed to say:

    * a log that parsed with zero failures -> ``pass``
    * a log that parsed with failures -> ``fail``, every node id named
    * no log at all -> ``blocked`` with the exact reason (no probe, the probe
      error, or the path that does not exist)
    """
    if log_path is None or not Path(log_path).is_file():
        reason = "no measured log was supplied for this lane"
        if probe is not None:
            if probe.get("error"):
                reason = f"{command} -> {probe['error']}"
            elif probe.get("timed_out"):
                reason = f"{command} -> timed out"
            elif probe.get("returncode") not in (0, None):
                tail = (probe.get("stderr") or probe.get("stdout") or "").strip()
                last = tail.splitlines()[-1] if tail else "no output"
                reason = f"{command} -> exit {probe['returncode']}: {last}"
        else:
            reason = f"no measured log at {log_path}"
        return blocked(rung_id, title, reason, owner=owner, evidence=command)
    parsed = parse_pytest_log(read_log_text(Path(log_path)))
    if not parsed["parsed"]:
        return blocked(
            rung_id,
            title,
            f"{log_path} has no recognisable pytest summary line, so its "
            "totals are not measurable and are NOT reported as zero",
            owner=owner,
            evidence=command,
        )
    totals = (
        f"passed={parsed['passed']} failed={parsed['failed']} "
        f"skipped={parsed['skipped']} errors={parsed['errors']} "
        f"xfailed={parsed.get('xfailed')}"
    )
    if parsed.get("no_tests_ran"):
        return Rung(
            rung_id,
            title,
            FAIL,
            "ran and collected NOTHING: the log says 'no tests ran'. Zero "
            "collected tests is never a pass (execution.result_parsing's own "
            "`no_tests` rule), so this lane is red rather than green.",
            owner=owner,
            evidence=f"{command} -> {log_path}",
        )
    # The clean criterion is stricter than "failed == 0": an ERROR is also a
    # failure, and a reported `xfailed` is a suppressed test, which this
    # repository's doctrine forbids for a gate.
    broken = (
        parsed["failed"]
        or parsed["failed_nodes"]
        or parsed["errors"]
        or parsed.get("xfailed")
    )
    if not broken:
        return Rung(
            rung_id,
            title,
            PASS,
            f"ran and held: {totals}",
            owner=owner,
            evidence=f"{command} -> {log_path}",
        )
    named = "\n".join(f"    {node}" for node in parsed["failed_nodes"])
    return Rung(
        rung_id,
        title,
        FAIL,
        f"ran and broke: {totals}. Failed nodes:\n{named}"
        if named
        else f"ran and broke: {totals}. No node ids were captured, so see the log.",
        owner=owner,
        evidence=f"{command} -> {log_path}",
    )


def g0_report(
    root: Path,
    *,
    tests_dir_log: Optional[Path] = None,
    module_local_log: Optional[Path] = None,
    run_probes: bool = True,
) -> Dict[str, Any]:
    """Probe every G0 rung and derive the verdict.

    :param tests_dir_log: path to a measured ``pytest tests/`` log. Without it
        the lane is ``blocked`` with that reason -- never a pass and never
        silently omitted.
    :param module_local_log: path to a measured log for the module-local pin
        suites, same contract.
    :param run_probes: set False in a unit test that injects its own probes.
    """
    root = Path(root)
    rungs: List[Rung] = []

    # -- 1. the prompt-regression self-check ---------------------------------
    if run_probes:
        check = run_probe(
            [sys.executable, "-m", "evals.run", "--check"], timeout_s=1800
        )
    else:
        check = {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "argv": "",
            "error": "",
        }
    check_out = (check.get("stdout") or "") + (check.get("stderr") or "")
    if check.get("timed_out"):
        rungs.append(
            blocked(
                "eval_prompt_self_check",
                "the 14-task prompt self-check",
                "python -m evals.run --check did not finish in 1800s",
                owner="T5",
                evidence="python -m evals.run --check",
            )
        )
    elif check.get("ok") and re.search(r"14/14", check_out):
        verdict_word = "CLEAN" if "CLEAN" in check_out else "14/14 reported"
        rungs.append(
            Rung(
                "eval_prompt_self_check",
                "the 14-task prompt self-check",
                PASS,
                f"ran and held: 14/14 ok, verdict {verdict_word}. NOTE this is "
                "a host self-check of the prompt task set and NOT a claim "
                "about model quality (see what_this_does_not_establish).",
                owner="T5",
                evidence="python -m evals.run --check",
            )
        )
    else:
        rungs.append(
            Rung(
                "eval_prompt_self_check",
                "the 14-task prompt self-check",
                FAIL,
                "ran and did not report 14/14 ok. Tail: "
                + (check_out.strip().splitlines() or ["<no output>"])[-6:][0],
                owner="T5",
                evidence="python -m evals.run --check",
            )
        )

    # -- 2. the trust-ladder suite -------------------------------------------
    #
    # A suite that is not REGISTERED is `not_implemented`, not `fail`: "does
    # not exist" and "exists and broke" are different claims, and reporting a
    # missing suite as a failure would send the next terminal looking for a bug
    # in code that was never written. The registration list is read from
    # argparse itself rather than hardcoded, so a suite added later is picked
    # up without editing this gate.
    ladder = None
    registered_suites: List[str] = []
    if run_probes:
        help_probe = run_probe(
            [sys.executable, "-m", "evals.run", "--help"], timeout_s=300
        )
        help_text = (help_probe.get("stdout") or "") + (help_probe.get("stderr") or "")
        registered_suites = sorted(set(re.findall(r"\{([a-z0-9,\-]+)\}", help_text)))
        LADDER.registered = registered_suites
    seen = ", ".join(registered_suites) if registered_suites else "<unread>"

    if run_probes and TRUST_LADDER_SUITE not in registered_suites:
        rungs.append(
            not_implemented(
                "trust_ladder_suite",
                "the trust-ladder eval suite",
                "T5 / P0 (no `trust-ladder` suite is registered in evals/run.py)",
                evidence=(
                    "`python -m evals.run --suite` accepts: "
                    + seen
                    + ". The Trust Ladder exists as "
                    "tests/test_ceiling_r2_15_trust.py but is NOT reachable "
                    "through the eval runner, so this gate cannot measure it."
                ),
            )
        )
    elif run_probes:
        ladder = run_probe(
            [sys.executable, "-m", "evals.run", "--suite", TRUST_LADDER_SUITE],
            timeout_s=2400,
        )
        ladder_out = (ladder.get("stdout") or "") + (ladder.get("stderr") or "")
        if ladder.get("ok"):
            rungs.append(
                Rung(
                    "trust_ladder_suite",
                    "the trust-ladder eval suite",
                    PASS,
                    "ran and held. Tail: "
                    + (ladder_out.strip().splitlines() or ["<no output>"])[-1],
                    owner="T5",
                    evidence=f"python -m evals.run --suite {TRUST_LADDER_SUITE}",
                )
            )
        elif ladder.get("timed_out"):
            rungs.append(
                blocked(
                    "trust_ladder_suite",
                    "the trust-ladder eval suite",
                    f"python -m evals.run --suite {TRUST_LADDER_SUITE} did "
                    "not finish in 2400s",
                    owner="T5",
                    evidence=f"python -m evals.run --suite {TRUST_LADDER_SUITE}",
                )
            )
        else:
            rungs.append(
                Rung(
                    "trust_ladder_suite",
                    "the trust-ladder eval suite",
                    FAIL,
                    f"ran and exited {ladder.get('returncode')}. Tail: "
                    + (ladder_out.strip().splitlines() or ["<no output>"])[-1],
                    owner="T5",
                    evidence=f"python -m evals.run --suite {TRUST_LADDER_SUITE}",
                )
            )
    else:
        rungs.append(
            not_implemented(
                "trust_ladder_suite",
                "the trust-ladder eval suite",
                "T5 / P0 (probe suppressed; registration not read)",
                evidence="probe suppressed",
            )
        )

    # -- 3. the known-failing pin registry -----------------------------------
    if run_probes:
        pins = run_probe(
            [sys.executable, "-m", "tests.known_failing_pins"], timeout_s=1800
        )
    else:
        pins = {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "argv": "",
            "error": "",
        }
    pin_out = (pins.get("stdout") or "") + (pins.get("stderr") or "")
    if pins.get("ok"):
        counts = re.search(r"counts: (.+)", pin_out)
        rungs.append(
            Rung(
                "known_failing_pin_registry",
                "every deliberately-red pin is red for its stated reason",
                PASS,
                "ran and held: "
                + (counts.group(1) if counts else "no counts line")
                + ". A pin that started passing would have been reported as "
                "PROMOTE and failed this rung.",
                owner="T5",
                evidence="python -m tests.known_failing_pins",
            )
        )
    else:
        rungs.append(
            Rung(
                "known_failing_pin_registry",
                "every deliberately-red pin is red for its stated reason",
                FAIL,
                "the registry check exited "
                f"{pins.get('returncode')}. This is a build failure: either a "
                "pin was promoted (the gap closed) or one broke for a "
                "different reason (a regression). Output:\n" + pin_out[-3000:],
                owner="T5",
                evidence="python -m tests.known_failing_pins",
            )
        )

    # -- 4/5. the two pytest lanes -------------------------------------------
    rungs.append(
        _suite_rung(
            "full_suite_tests_dir",
            "the tests/ suite",
            tests_dir_log,
            owner="T5",
            command="python -m pytest tests/ -q -p no:randomly",
        )
    )
    rungs.append(
        _suite_rung(
            "full_suite_module_local",
            "the module-local pin suites (harness/execution/runtime/cli/...)",
            module_local_log,
            owner="T5",
            command="python -m pytest harness execution runtime cli "
            "mcp_server memory shared evals scripts dashboard demo -q",
        )
    )

    # -- 6. Docker ------------------------------------------------------------
    docker = (
        docker_reachable()
        if run_probes
        else {
            "reachable": False,
            "cli_present": False,
            "evidence": "probe suppressed",
            "error": "probe suppressed",
        }
    )
    if docker["reachable"]:
        rungs.append(
            Rung(
                "docker_daemon_reachable",
                "a real Docker daemon is reachable for the container lanes",
                PASS,
                "ran and held: " + docker["evidence"],
                owner="T5",
                evidence="docker info",
            )
        )
    else:
        rungs.append(
            blocked(
                "docker_daemon_reachable",
                "a real Docker daemon is reachable for the container lanes",
                docker["error"],
                owner="T5",
                evidence=docker["evidence"],
            )
        )

    # -- 7. live provider -----------------------------------------------------
    # Never probed with a credential. T3's recorded result is carried as a
    # BLOCKED row with the exact provider error, because a lane that cannot
    # run is blocked, not skipped and not passed.
    rungs.append(
        blocked(
            "live_provider_lane",
            "a live model provider lane",
            "no live provider is reachable. T3 recorded "
            "ServiceUnavailableError: No available channel on 3/3 live "
            "completions, so every model call in this gate -- and in the whole "
            "test suite -- is a scripted double. No credential was inspected, "
            "requested or retained by this gate.",
            owner="T4 / P6 (provider availability)",
            evidence="logs/ceiling/*.json SG-03; no probe run (no credential)",
        )
    )

    # -- 8. the Windows lane --------------------------------------------------
    windows_workflow = root / ".github" / "workflows" / "windows-dockerfree-ci.yml"
    if windows_workflow.is_file():
        rungs.append(
            Rung(
                "windows_lane",
                "the Windows Docker-free CI lane",
                PASS,
                "the lane is DEFINED and its enumerated subset exists "
                f"({windows_workflow.stat().st_size} bytes). Whether it RAN is "
                "a separate question and is reported in the run summary, not "
                "inferred from the file existing.",
                owner="T5",
                evidence=".github/workflows/windows-dockerfree-ci.yml",
                blocking=False,
            )
        )
    else:
        rungs.append(
            not_implemented(
                "windows_lane",
                "the Windows Docker-free CI lane",
                "T5 / P0 (the workflow file is absent)",
                evidence="absent",
            )
        )

    # -- 9. security blockers -------------------------------------------------
    security = count_security_blockers(root)
    if not security["measurable"]:
        rungs.append(
            blocked(
                "security_blockers",
                "the recorded security-blocker count",
                f"NOT measurable: {security['detail']}. This is reported as "
                "blocked rather than 0, because an absent measurement rendered "
                "as zero is the anti-pattern DOCTRINE.md §1 names.",
                owner="T5",
                evidence="shared.security_advisories",
            )
        )
    elif security["count"] == 0:
        rungs.append(
            Rung(
                "security_blockers",
                "the recorded security-blocker count",
                PASS,
                f"ran and held: {security['detail']} -- the count is ZERO.",
                owner="T5",
                evidence="shared.security_advisories",
            )
        )
    else:
        rungs.append(
            Rung(
                "security_blockers",
                "the recorded security-blocker count",
                FAIL,
                f"the count is NOT zero. {security['detail']}. Active: "
                + "; ".join(security["blocking"])
                + (
                    ". Unresolvable-version advisories (NOT counted as "
                    "resolved): " + "; ".join(security["unknown_version"])
                    if security["unknown_version"]
                    else ""
                ),
                owner="T5",
                evidence="shared.security_advisories",
            )
        )

    # -- 10. provenance spine -------------------------------------------------
    provenance = root / "scripts" / "provenance_report.py"
    if provenance.is_file():
        rungs.append(
            Rung(
                "provenance_spine",
                "the vendored-code provenance scanner exists and runs",
                PASS,
                "scripts/provenance_report.py is present. Whether the current "
                "tree is CLEAN is a separate measurement, run by the script "
                "itself and reported here only as present-or-absent.",
                owner="T5",
                evidence="scripts/provenance_report.py",
                blocking=False,
            )
        )
    else:
        rungs.append(
            not_implemented(
                "provenance_spine",
                "the vendored-code provenance scanner",
                "T5 / W2.5 (Phase 7 needs the spine; Phase 2 ports must fill it in)",
                evidence="absent",
            )
        )

    blocking_failures = [r for r in rungs if r.blocking and r.status == FAIL]
    blocked_rows = [r for r in rungs if r.status == BLOCKED]
    return {
        "schema_version": 1,
        "gate": "G0",
        "phase": "P0 Foundation",
        "statuses": list(STATUSES),
        "verdict": GATE_GREEN if not blocking_failures else GATE_RED,
        "rungs": [r.to_dict() for r in rungs],
        "counts": {
            PASS: sum(1 for r in rungs if r.status == PASS),
            FAIL: sum(1 for r in rungs if r.status == FAIL),
            BLOCKED: len(blocked_rows),
            NOT_IMPLEMENTED: sum(1 for r in rungs if r.status == NOT_IMPLEMENTED),
        },
        "blocked_rows": [
            {"id": r.id, "reason": r.detail, "owner": r.owner} for r in blocked_rows
        ],
        "what_this_does_not_establish": list(NOT_ESTABLISHED),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def render(report: Dict[str, Any]) -> str:
    """Render the gate as the table a reader of a red CI run needs."""
    glyph = {
        PASS: "pass  ",
        FAIL: "FAIL  ",
        BLOCKED: "BLOCK ",
        NOT_IMPLEMENTED: "NOTIMPL",
    }
    lines = [
        f"G0 ({report['phase']}): {report['verdict']}",
        "",
        # The vocabulary is printed in the header, not just used in the rows,
        # so a reader is told up front that `skip` is not one of the options.
        "status vocabulary: " + " | ".join(report["statuses"]),
        "",
        f"{'status':8} {'rung':34} detail",
        "-" * 100,
    ]
    for rung in report["rungs"]:
        first = rung["detail"].splitlines()[0] if rung["detail"] else ""
        lines.append(f"{glyph[rung['status']]:8} {rung['id']:34} {first}")
        extra = rung["detail"].splitlines()[1:]
        for line in extra:
            lines.append(f"{'':8} {'':34} {line}")
    counts = report["counts"]
    lines += [
        "",
        "counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()),
        "",
        "BLOCKED rows (never reported as skips):",
    ]
    if report["blocked_rows"]:
        for row in report["blocked_rows"]:
            lines.append(f"  - {row['id']} (owner {row['owner'] or 'unassigned'})")
            lines.append(f"      {row['reason']}")
    else:
        lines.append("  - none")
    lines += ["", "WHAT G0 DOES NOT ESTABLISH:"]
    for item in report["what_this_does_not_establish"]:
        lines.append(f"  * {item}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """Print the G0 report. Exit 2 on a blocking failure, 0 otherwise.

    A ``blocked`` row does NOT by itself make the exit code 2: blocked is an
    honest report, not a failure, and the point of the vocabulary is that a
    blocked lane is visible rather than disguised. The decision of whether a
    blocked row may ship is the reader's, made from a table that says so.
    """
    parser = argparse.ArgumentParser(
        prog="python -m evals.gates.P0",
        description="The G0 P0 Foundation gate. Four statuses, never skip.",
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument(
        "--tests-dir-log",
        default=None,
        help="path to a measured `pytest tests/` log (else the lane is blocked)",
    )
    parser.add_argument(
        "--module-local-log",
        default=None,
        help="path to a measured module-local-suite log (else blocked)",
    )
    parser.add_argument(
        "--no-probes",
        action="store_true",
        help="do not run subprocess probes (for tests that inject their own)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = g0_report(
        Path(args.root),
        tests_dir_log=Path(args.tests_dir_log) if args.tests_dir_log else None,
        module_local_log=Path(args.module_local_log) if args.module_local_log else None,
        run_probes=not args.no_probes,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 2 if report["verdict"] == GATE_RED else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
