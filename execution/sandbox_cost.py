"""Measure and publish what the sandbox actually costs.

This exists because the honest number for the Trust Ladder's rung 1 ("the
sandbox never mutates outside its declared roots") has to include cost, and
because `execution/AGENTS.md` records that a bare ``os.walk`` of THIS
repository takes **362 s**. If the sandbox's setup path did a comparable
walk, every tool call would pay it and nobody would notice, because the cost
would be spread across a call that already takes seconds.

Four measurements, each with the number and the reasoning:

1. **Container creation, cold and warm.** A fresh ``docker run`` per call is
   the product's design, so this is a fixed per-call cost, not a startup cost.
2. **Per-call overhead versus a local subprocess.** The delta is the price of
   the boundary. It is a *differential* on purpose: it cancels the host's
   process-spawn cost, which is large on Windows and would otherwise make the
   container look cheap or expensive for the wrong reason.
3. **Whether a CODE EDIT triggers an image build.** It must not. The image
   fingerprint is dependency manifests only, so editing a source file must not
   invalidate it. This is checked by comparing the resolved image tag before
   and after a real source edit and then timing ``ensure_image``; a rebuild
   would show as a tag change or a multi-second cold build.
4. **Any filesystem walk on the per-call path**, with its directory count and
   its duration. Measured by counting and timing every ``os.scandir`` /
   ``os.listdir`` / ``os.walk`` entry made *inside* one real
   ``execute_sandboxed`` call. The acceptance bar is stated: a per-call walk
   over **1,000 entries or 1 s** is removed or capped, with the number in a
   comment. Measurement #4 is the one that decides whether that bar is met.

Nothing here is claimed without a number. Where a measurement could not run,
the row says ``blocked`` and why.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from execution.sandbox import (
    SandboxUnavailableError,
    _dep_image_tag,
    docker_available,
    ensure_image,
    execute_sandboxed,
)
from execution.workspace import execute_local

#: The acceptance bar for measurement #4, quoted from the brief so the number
#: and the threshold live together and a future reader cannot move one without
#: seeing the other.
WALK_ENTRY_BAR = 1_000
WALK_SECONDS_BAR = 1.0

#: How many samples each timing takes. Three is the minimum that can say
#: "median" without pretending; on a four-terminal shared host more samples
#: cost more than they buy.
SAMPLES = 3


@dataclass
class Measurement:
    """One measured number, with the conditions it was measured under."""

    name: str
    value: Optional[float] = None
    unit: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    blocked: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocked and self.value is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "blocked": self.blocked,
            "note": self.note,
            "detail": dict(self.detail),
        }


# ---------------------------------------------------------------------------
# #4 first, because it is the one that can change the design
# ---------------------------------------------------------------------------


class _DirectoryProbe:
    """Count and time every directory enumeration inside a block.

    Patches ``os.scandir``, ``os.listdir`` and ``os.walk`` at the ``os``
    module level. That is sufficient because ``os.walk`` resolves
    ``scandir`` from its module globals at call time, so a walk is counted
    through its own ``scandir`` calls rather than needing a separate counter.

    Counts ENTRIES (``scandir`` returns one handle per directory, so an entry
    is one directory opened), which is the unit the brief's 1,000-entry bar is
    written in.
    """

    def __init__(self) -> None:
        self.entries = 0
        self.seconds = 0.0
        self.paths: List[str] = []
        self._scandir = os.scandir
        self._listdir = os.listdir
        self._walk = os.walk
        self._depth = 0

    def __enter__(self) -> "_DirectoryProbe":
        probe = self
        self._depth += 1
        if self._depth > 1:  # nested: only the outermost block measures
            return self

        def scandir(path="."):  # type: ignore[no-untyped-def]
            started = time.perf_counter()
            try:
                probe.entries += 1
                if len(probe.paths) < 40:
                    probe.paths.append(str(path)[:200])
                return probe._scandir(path)
            finally:
                probe.seconds += time.perf_counter() - started

        def listdir(path="."):  # type: ignore[no-untyped-def]
            started = time.perf_counter()
            try:
                probe.entries += 1
                return probe._listdir(path)
            finally:
                probe.seconds += time.perf_counter() - started

        os.scandir = scandir  # type: ignore[assignment]
        os.listdir = listdir  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._depth -= 1
        if self._depth > 0:
            return
        os.scandir = self._scandir  # type: ignore[assignment]
        os.listdir = self._listdir  # type: ignore[assignment]

    def summary(self) -> Dict[str, Any]:
        return {
            "directory_entries": int(self.entries),
            "seconds": round(float(self.seconds), 4),
            "sample_paths": list(self.paths[:20]),
        }


def measure_per_call_walk(root: Path, timeout_s: int = 90) -> Measurement:
    """Count directory enumerations inside ONE real `execute_sandboxed` call.

    This is measurement #4 and it is the one the brief's 1000-entry / 1 s bar
    applies to. `ensure_image` is called FIRST and OUTSIDE the probe, so what
    is measured is the per-call path and not the one-time image fingerprint —
    conflating the two would hide exactly the cost this is looking for.
    """
    measure = Measurement(
        name="per_call_filesystem_walk",
        unit="directory_entries",
        note=(
            f"bar: <= {WALK_ENTRY_BAR} entries and <= {WALK_SECONDS_BAR}s per "
            "sandboxed call; measured around one real execute_sandboxed call "
            "with ensure_image warmed outside the probe"
        ),
    )
    if not docker_available():
        measure.blocked = "docker daemon not reachable"
        return measure
    try:
        ensure_image(str(root))  # outside the probe on purpose
        with _DirectoryProbe() as probe:
            started = time.perf_counter()
            result = execute_sandboxed(str(root), "echo walked", timeout_s)
            wall = time.perf_counter() - started
    except SandboxUnavailableError as exc:
        measure.blocked = f"docker daemon not reachable: {exc}"
        return measure
    facts = probe.summary()
    facts["call_wall_s"] = round(wall, 4)
    facts["container_exit"] = result.exit_code
    measure.value = float(facts["directory_entries"])
    measure.detail = facts
    over_entries = facts["directory_entries"] > WALK_ENTRY_BAR
    over_seconds = facts["seconds"] > WALK_SECONDS_BAR
    measure.note += (
        f" -- OVER BAR: entries={facts['directory_entries']} > {WALK_ENTRY_BAR}, "
        f"walk={facts['seconds']}s > {WALK_SECONDS_BAR}s"
        if (over_entries or over_seconds)
        else f" -- WITHIN BAR (entries {facts['directory_entries']}, "
        f"walk {facts['seconds']}s)"
    )
    return measure


# ---------------------------------------------------------------------------
# #1 and #2 — timings
# ---------------------------------------------------------------------------


def _time(fn: Callable[[], Any], samples: int = SAMPLES) -> List[float]:
    out: List[float] = []
    for _ in range(max(1, samples)):
        started = time.perf_counter()
        fn()
        out.append(time.perf_counter() - started)
    return out


def measure_container_creation(root: Path, timeout_s: int = 90) -> List[Measurement]:
    """Measure #1: the first container's cost and the warm ones.

    "Cold" here means the FIRST call in this process for this repository's
    image: the daemon's container-create path is exercised with a cold page
    cache for the container metadata even though the image layers are warm.
    "Warm" is the median of the following samples. Both are the same fresh
    ``--rm`` container the product always creates, so the difference is cache
    warmth and nothing else — a warm-container REUSE path exists in
    ``execution/warm_sandbox.py`` and is a different design, measured
    separately, and is not wired into the default call.
    """
    cold = Measurement(
        name="container_creation_cold",
        unit="s",
        note="first execute_sandboxed call in this process against a warm IMAGE",
    )
    warm = Measurement(
        name="container_creation_warm_median",
        unit="s",
        note=f"median of {SAMPLES - 1} subsequent calls; same fresh --rm container",
    )
    if not docker_available():
        cold.blocked = warm.blocked = "docker daemon not reachable"
        return [cold, warm]
    try:
        ensure_image(str(root))
        first = _time(lambda: execute_sandboxed(str(root), "echo cold", timeout_s), 1)
        rest = _time(
            lambda: execute_sandboxed(str(root), "echo warm", timeout_s), SAMPLES - 1
        )
    except SandboxUnavailableError as exc:
        cold.blocked = warm.blocked = f"docker daemon not reachable: {exc}"
        return [cold, warm]
    cold.value = round(first[0], 4)
    warm.value = round(statistics.median(rest), 4)
    cold.detail = {"samples_s": [round(v, 4) for v in first]}
    warm.detail = {"samples_s": [round(v, 4) for v in rest]}
    return [cold, warm]


def measure_per_call_overhead(root: Path, timeout_s: int = 90) -> Measurement:
    """Measure #2: sandboxed call minus local subprocess call.

    A DIFFERENTIAL, deliberately. The host's own process-spawn cost on Windows
    is tens of milliseconds and on Linux a few, so an absolute number would
    measure the host rather than the boundary. Both arms run the same trivial
    command through the same code path shape, so what remains is the container
    boundary.

    The local arm is the `SafeToolBackend`'s own `execute_local`, which is
    also where the ingress runs, so this number is the honest end-to-end
    per-call cost of each boundary as the product actually pays it.
    """
    measure = Measurement(
        name="per_call_overhead_vs_local",
        unit="s",
        note=(
            "median sandboxed call minus median local subprocess call, same "
            "trivial command; a differential so it measures the CONTAINER "
            "boundary and not the host's process-spawn cost"
        ),
    )
    if not docker_available():
        measure.blocked = "docker daemon not reachable"
        return measure
    try:
        ensure_image(str(root))
        sandboxed = _time(lambda: execute_sandboxed(str(root), "echo x", timeout_s))
        local = _time(lambda: execute_local(str(root), "echo x", timeout_s=60))
    except SandboxUnavailableError as exc:
        measure.blocked = f"docker daemon not reachable: {exc}"
        return measure
    med_s = statistics.median(sandboxed)
    med_l = statistics.median(local)
    measure.value = round(med_s - med_l, 4)
    measure.detail = {
        "sandboxed_samples_s": [round(v, 4) for v in sandboxed],
        "sandboxed_median_s": round(med_s, 4),
        "local_samples_s": [round(v, 4) for v in local],
        "local_median_s": round(med_l, 4),
        "ratio": round(med_s / med_l, 2) if med_l > 0 else None,
    }
    return measure


# ---------------------------------------------------------------------------
# #3 — is an image build triggered by a code edit?
# ---------------------------------------------------------------------------


def measure_code_edit_build(root: Path) -> Measurement:
    """Measure #3: a source edit must NOT change the image fingerprint.

    The claim under test is the one `execution/AGENTS.md` has claimed since
    Round 3: the dependency image fingerprint is dependency manifests only, so
    "code edits never trigger rebuilds". A source edit is made for real, the
    resolved image tag is re-read, and `ensure_image` is timed. Two independent
    observations, because either alone is weak:

    * the tag is IDENTICAL before and after (proves the fingerprint ignores
      source); and
    * `ensure_image` after the edit costs about the same as before it (proves
      no build was kicked off, which a tag collision could otherwise hide if
      the build had already been triggered by something else).
    """
    measure = Measurement(
        name="code_edit_triggers_image_build",
        unit="rebuilds",
        note=(
            "edit a real source file, re-resolve the dependency image tag, and "
            "time ensure_image; a code edit must change neither the tag nor "
            "the build cost"
        ),
    )
    repo = root
    (repo / "mymod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    before_tag = _dep_image_tag(str(repo))
    try:
        t0 = time.perf_counter()
        ensure_image(str(repo))
        warm_build_s = time.perf_counter() - t0
    except SandboxUnavailableError as exc:
        measure.blocked = f"docker daemon not reachable: {exc}"
        return measure

    # A real code edit: several source files, plus a new one, plus an edit
    # inside a subpackage. If any of these moved the fingerprint, this is
    # where it would show.
    (repo / "mymod.py").write_text(
        "VALUE = 2\n# edited by the cost probe\n", encoding="utf-8"
    )
    (repo / "extra.py").write_text("NEW = True\n", encoding="utf-8")
    (repo / "pkg").mkdir(exist_ok=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("X = 1\n", encoding="utf-8")
    after_tag = _dep_image_tag(str(repo))
    try:
        t0 = time.perf_counter()
        ensure_image(str(repo))
        post_edit_build_s = time.perf_counter() - t0
    except SandboxUnavailableError as exc:
        measure.blocked = f"docker daemon not reachable: {exc}"
        return measure

    measure.value = 1.0 if after_tag != before_tag else 0.0
    measure.detail = {
        "tag_before_edit": before_tag,
        "tag_after_edit": after_tag,
        "tag_changed": after_tag != before_tag,
        "ensure_image_warm_s": round(warm_build_s, 4),
        "ensure_image_after_edit_s": round(post_edit_build_s, 4),
        "files_edited": ["mymod.py", "extra.py", "pkg/__init__.py", "pkg/core.py"],
        "manifest_untouched": True,
    }
    return measure


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _fixture(parent: Path) -> Path:
    root = parent / "cost"
    root.mkdir(parents=True, exist_ok=True)
    (root / "mymod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    return root


def measure_all(*, timeout_s: int = 90, keep: bool = False) -> Dict[str, Any]:
    """Run all four measurements and return the machine-readable report."""
    parent = Path(tempfile.mkdtemp(prefix="neo-sandbox-cost-"))
    try:
        root = _fixture(parent)
        measurements: List[Measurement] = []
        measurements.extend(measure_container_creation(root, timeout_s))
        measurements.append(measure_per_call_overhead(root, timeout_s))
        measurements.append(measure_code_edit_build(root))
        measurements.append(measure_per_call_walk(root, timeout_s))
    finally:
        if not keep:
            shutil.rmtree(parent, ignore_errors=True)
    blocked = [m for m in measurements if not m.ok]
    walk = next((m for m in measurements if m.name == "per_call_filesystem_walk"), None)
    return {
        "measurements": [m.to_dict() for m in measurements],
        "walk_bar": {"entries": WALK_ENTRY_BAR, "seconds": WALK_SECONDS_BAR},
        "walk_within_bar": bool(
            walk
            and walk.ok
            and walk.detail.get("directory_entries", 0) <= WALK_ENTRY_BAR
            and walk.detail.get("seconds", 0.0) <= WALK_SECONDS_BAR
        ),
        "blocked": [m.name for m in blocked],
        "env": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "docker": docker_blocked_reason(),
        },
    }


def docker_blocked_reason() -> str:
    """The exact reason Docker measurements cannot run, or ""."""
    try:
        if docker_available():
            return ""
    except Exception as exc:  # pragma: no cover
        return f"{type(exc).__name__}: {exc}"
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError as exc:
        return f"docker daemon not reachable: {type(exc).__name__}: {exc}"
    if probe.returncode == 0 and (probe.stdout or "").strip():
        return ""
    return (
        "docker daemon not reachable: "
        + (((probe.stderr or "") + " " + (probe.stdout or "")).strip()[:400])
    )


def render(report: Dict[str, Any]) -> str:
    """Render the four measurements as plain text."""
    lines = ["sandbox cost, measured (not estimated)", ""]
    for item in report["measurements"]:
        lines.append(f"* {item['name']}  [{item['unit']}]")
        if item["blocked"]:
            lines.append(f"    BLOCKED: {item['blocked']}")
        else:
            lines.append(f"    value   : {item['value']}")
            for key, value in item["detail"].items():
                lines.append(f"    {key:<23}: {value}")
        if item["note"]:
            lines.append(f"    note    : {item['note']}")
        lines.append("")
    bar = report["walk_bar"]
    lines.append(
        f"per-call walk bar: <= {bar['entries']} entries / <= {bar['seconds']}s "
        f"-> {'WITHIN' if report['walk_within_bar'] else 'OVER / UNKNOWN'}"
    )
    if report["blocked"]:
        lines.append(f"blocked measurements: {', '.join(report['blocked'])}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point: ``python -m execution.sandbox_cost``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m execution.sandbox_cost",
        description="Measure the sandbox's real per-call cost and publish it.",
    )
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = measure_all(timeout_s=int(args.timeout), keep=bool(args.keep))
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    # Non-zero when a measurement is blocked: a missing number must not render
    # as a clean exit.
    return 0 if not report["blocked"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "SAMPLES",
    "WALK_ENTRY_BAR",
    "WALK_SECONDS_BAR",
    "Measurement",
    "main",
    "measure_all",
    "measure_code_edit_build",
    "measure_container_creation",
    "measure_per_call_overhead",
    "measure_per_call_walk",
    "render",
]
